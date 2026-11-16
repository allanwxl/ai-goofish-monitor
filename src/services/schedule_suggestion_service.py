"""
定时建议服务

为新任务挑选一个**不与其它任务冲突**的执行时间（cron 表达式）。

背景：多个定时任务会在同一时刻启动 Playwright 爬虫，争抢浏览器登录态、账号与
网络带宽，既拖慢彼此，也更容易触发闲鱼风控。因此新任务的触发点需要与已有任务
错开。

流程：
1. 采集所有启用任务在未来校验窗口内的触发时刻（已占用时段）；
2. 让 AI 在这些已占用时段之外挑一个合适的 cron；
3. 对 AI 返回的表达式做「可解析 + 频率匹配 + 不冲突」三重校验，不通过就把原因
   反馈给 AI 让它重写（最多 {@see MAX_AI_ATTEMPTS} 轮）；
4. AI 始终给不出可用结果时，退化为确定性的兜底算法：在低峰时段里挑一个空闲的
   整点偏分钟，保证功能不会因为模型不可用而彻底不可用。

执行频率由调用方指定（每周 / 每两天 / 每 3 天 / 每天，默认每周），它会同时影响
提示词、校验窗口与兜底算法：周期越长的规则，校验窗口必须看得越远，否则规则的
「下一次触发」会落在窗口之外被误判成永不触发。
"""
from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from apscheduler.util import astimezone

from src.core.cron_utils import build_cron_trigger, normalize_cron_expression
from src.domain.models.task import (
    DEFAULT_SCHEDULE_FREQUENCY,
    SCHEDULE_FREQUENCY_DAILY,
    SCHEDULE_FREQUENCY_EVERY_2_DAYS,
    SCHEDULE_FREQUENCY_EVERY_3_DAYS,
    SCHEDULE_FREQUENCY_WEEKLY,
    Task,
)
from src.infrastructure.external.ai_client import AIClient
from src.services.ai_response_parser import parse_ai_response_json

SCHEDULE_TIMEZONE = "Asia/Shanghai"

# 两个任务的触发时刻相差在 10 分钟以内即视为冲突：爬虫本身会持续占用账号和网络，
# 仅仅"分钟数不同"并不能真正错开资源。
CONFLICT_WINDOW_MINUTES = 10

# 校验冲突时向前看的时长（每天执行时的默认值）。
LOOKAHEAD_HOURS = 48

# 单个任务最多采样多少个触发点，避免 */1 这类高频表达式把样本撑爆。
# 需要覆盖最长校验窗口（每周执行时为 216 小时）里「每天一次」的任务，
# 否则繁忙时段会被截断，漏判跨周的真实冲突。
MAX_FIRE_SAMPLES_PER_TASK = 256

# 各执行频率的冲突校验窗口。周期越长的规则必须看得越远，否则规则的
# 「下一次触发」会落在窗口之外，被误判成「永远不会触发」。
FREQUENCY_LOOKAHEAD_HOURS: Dict[str, int] = {
    SCHEDULE_FREQUENCY_WEEKLY: 216,        # 9 天，保证覆盖下一次周触发
    SCHEDULE_FREQUENCY_EVERY_2_DAYS: 120,  # 5 天
    SCHEDULE_FREQUENCY_EVERY_3_DAYS: 144,  # 6 天
    SCHEDULE_FREQUENCY_DAILY: 48,
}

# 各执行频率的中文说明，写进提示词。
FREQUENCY_LABELS: Dict[str, str] = {
    SCHEDULE_FREQUENCY_WEEKLY: "每周执行一次",
    SCHEDULE_FREQUENCY_EVERY_2_DAYS: "每两天执行一次",
    SCHEDULE_FREQUENCY_EVERY_3_DAYS: "每 3 天执行一次",
    SCHEDULE_FREQUENCY_DAILY: "每天执行一次",
}

# 各执行频率要求的 cron 字段形态，写进提示词。
FREQUENCY_CRON_HINTS: Dict[str, str] = {
    SCHEDULE_FREQUENCY_WEEKLY: (
        "日字段为 *，周字段固定为某一天"
        "（0=周一, 1=周二, 2=周三, 3=周四, 4=周五, 5=周六, 6=周日）"
    ),
    SCHEDULE_FREQUENCY_EVERY_2_DAYS: "日字段为 */2，月份与周字段为 *",
    SCHEDULE_FREQUENCY_EVERY_3_DAYS: "日字段为 */3，月份与周字段为 *",
    SCHEDULE_FREQUENCY_DAILY: "日、月份、周三个字段都为 *",
}

# 各执行频率的 cron 示例，写进提示词。
FREQUENCY_CRON_EXAMPLES: Dict[str, str] = {
    SCHEDULE_FREQUENCY_WEEKLY: "17 3 * * 0",
    SCHEDULE_FREQUENCY_EVERY_2_DAYS: "17 3 */2 * *",
    SCHEDULE_FREQUENCY_EVERY_3_DAYS: "17 3 */3 * *",
    SCHEDULE_FREQUENCY_DAILY: "17 3 * * *",
}

# 周字段允许的取值（与调度器一致：0=周一 ... 6=周日）。
_WEEKDAY_VALUES = frozenset("0123456")

# 提示词里每个任务最多列出多少个占用时刻，其余用「还有 N 次触发」概括。
PER_TASK_SLOT_SAMPLES = 6

# AI 最多尝试几轮（每轮把冲突信息反馈给模型）。
MAX_AI_ATTEMPTS = 3

# 单次 AI 调用的输出预算。推理模型会先输出推理过程，预算过小正文会被截断。
SCHEDULE_MAX_OUTPUT_TOKENS = 4096

# 兜底选点时优先尝试的低峰时段（凌晨业务压力最小）。
FALLBACK_HOURS = (2, 3, 4, 5, 1, 6, 23, 22, 7, 0, 8, 9)

# 兜底选点时优先尝试的分钟数。刻意避开整点（xx:00），减少和别的任务撞点。
FALLBACK_MINUTES = (17, 23, 37, 41, 53, 7, 11, 29, 43, 31, 19, 13)

# 从模型回复里捞 cron：字段只允许 cron 语法字符，段数 5~6。
_CRON_SEARCH_RE = re.compile(
    r"(?<![\w@])([0-9*/,\-]{1,20}(?:\s+[0-9*/,\-]{1,20}){4,5})(?![\w*])"
)

# 匹配 "cron: 17 3 * * *" 这类带前缀的写法。
_CRON_PREFIX_RE = re.compile(r"(?:cron|定时|表达式)\s*[:：=]\s*", re.IGNORECASE)


def normalize_frequency(value: Optional[str]) -> str:
    """归一化外部传入的执行频率；未知取值回落到默认频率（每周）。"""
    key = str(value or "").strip().lower()
    if key in FREQUENCY_LABELS:
        return key
    return DEFAULT_SCHEDULE_FREQUENCY


def _day_step(day_field: str) -> Optional[int]:
    """从 cron 的日字段里解析步长（*/2 -> 2）；不是步长写法返回 None。"""
    if not day_field.startswith("*/"):
        return None
    try:
        return int(day_field[2:])
    except ValueError:
        return None


def cron_matches_frequency(cron: Optional[str], frequency: str) -> bool:
    """判断 cron 的执行周期是否符合用户选择的频率。

    AI 经常无视频率要求直接给出「每天一次」，这里做一次硬校验，
    不符合就把期望的字段形态反馈回去让它重写。
    """
    normalized = normalize_cron_expression(cron)
    if not normalized:
        return False

    parts = normalized.split()
    if len(parts) != 5:
        return False

    _minute, _hour, day, month, day_of_week = parts

    if frequency == SCHEDULE_FREQUENCY_DAILY:
        return day == "*" and month == "*" and day_of_week == "*"
    if frequency == SCHEDULE_FREQUENCY_EVERY_2_DAYS:
        return _day_step(day) == 2 and month == "*" and day_of_week == "*"
    if frequency == SCHEDULE_FREQUENCY_EVERY_3_DAYS:
        return _day_step(day) == 3 and month == "*" and day_of_week == "*"
    if frequency == SCHEDULE_FREQUENCY_WEEKLY:
        return day == "*" and month == "*" and day_of_week in _WEEKDAY_VALUES
    return False


class ScheduleSuggestionError(RuntimeError):
    """无法给出可用的定时建议。"""


@dataclass
class BusySlot:
    """一个已被其它任务占用的触发时刻。"""

    when: datetime
    task_name: str


@dataclass
class ExistingSchedule:
    """已有任务的定时规则及其触发时刻。"""

    task_id: Optional[int]
    task_name: str
    cron: str
    fire_times: List[datetime]

    def describe(self) -> str:
        return f"任务「{self.task_name}」cron={self.cron}"


@dataclass
class ScheduleSuggestion:
    """定时建议结果。"""

    cron: str
    reason: str
    source: str  # "ai" | "fallback"
    avoided: List[str]
    considered_tasks: int

    def to_payload(self) -> Dict:
        return {
            "cron": self.cron,
            "reason": self.reason,
            "source": self.source,
            "avoided": self.avoided,
            "considered_tasks": self.considered_tasks,
        }


class ScheduleSuggestionService:
    """用 AI 为任务挑选避开其它任务的执行时间。"""

    def __init__(
        self,
        ai_client: Optional[AIClient] = None,
        timezone_name: str = SCHEDULE_TIMEZONE,
    ):
        self._ai_client = ai_client
        self.timezone = astimezone(timezone_name)

    # ------------------------------------------------------------------
    # 采集与计算
    # ------------------------------------------------------------------
    def collect_fire_times(
        self,
        cron: Optional[str],
        start: datetime,
        limit: int = MAX_FIRE_SAMPLES_PER_TASK,
        lookahead_hours: int = LOOKAHEAD_HOURS,
    ) -> List[datetime]:
        """计算某个 cron 在未来 lookahead_hours 小时内的全部触发时刻。"""
        normalized = normalize_cron_expression(cron)
        if not normalized:
            return []

        try:
            trigger = build_cron_trigger(normalized, timezone=self.timezone)
        except ValueError:
            return []

        deadline = start + timedelta(hours=lookahead_hours)
        fires: List[datetime] = []
        current = start
        previous: Optional[datetime] = None
        for _ in range(limit):
            try:
                # 必须带上 previous_fire_time，否则 CronTrigger 会把上一次触发时刻
                # 本身重新算作下一次，采样结果出现大量重复。
                next_fire = trigger.get_next_fire_time(previous, current)
            except Exception:
                break
            if next_fire is None or next_fire > deadline:
                break
            fires.append(next_fire)
            previous = next_fire
            current = next_fire
        return fires

    def collect_existing_schedules(
        self,
        tasks: Sequence[Task],
        exclude_task_id: Optional[int] = None,
        now: Optional[datetime] = None,
        lookahead_hours: int = LOOKAHEAD_HOURS,
    ) -> List[ExistingSchedule]:
        """采集所有启用且配置了定时规则的任务。"""
        start = now or datetime.now(self.timezone)
        schedules: List[ExistingSchedule] = []

        for task in tasks:
            if task.id is not None and task.id == exclude_task_id:
                continue
            if not task.enabled:
                continue
            if not task.cron:
                continue

            fire_times = self.collect_fire_times(
                task.cron, start, lookahead_hours=lookahead_hours
            )
            if not fire_times:
                continue

            schedules.append(
                ExistingSchedule(
                    task_id=task.id,
                    task_name=task.task_name,
                    cron=str(task.cron),
                    fire_times=fire_times,
                )
            )
        return schedules

    @staticmethod
    def _busy_slots(schedules: Sequence[ExistingSchedule]) -> List[BusySlot]:
        slots = [
            BusySlot(when=fire_time, task_name=schedule.task_name)
            for schedule in schedules
            for fire_time in schedule.fire_times
        ]
        slots.sort(key=lambda slot: slot.when)
        return slots

    def find_conflicts(
        self,
        candidate_fires: Sequence[datetime],
        busy_slots: Sequence[BusySlot],
    ) -> List[Tuple[datetime, BusySlot]]:
        """找出候选触发时刻与已占用时段的重叠。"""
        if not candidate_fires or not busy_slots:
            return []

        window = timedelta(minutes=CONFLICT_WINDOW_MINUTES)
        # 内部自行排序，不依赖调用方传入的是有序列表（二分查找的前提）
        ordered = sorted(busy_slots, key=lambda slot: slot.when)
        busy_times = [slot.when for slot in ordered]
        conflicts: List[Tuple[datetime, BusySlot]] = []

        for fire in candidate_fires:
            index = bisect_left(busy_times, fire - window)
            while index < len(ordered) and busy_times[index] <= fire + window:
                conflicts.append((fire, ordered[index]))
                index += 1
        return conflicts

    def _fallback_crons(
        self,
        frequency: str,
        start: datetime,
        minute: int,
        hour: int,
    ) -> List[str]:
        """给出某个「几点几分」在各执行频率下的 cron 写法。

        每周执行时会把 7 个星期几都列出来，并从「下一个星期几」开始轮转，
        避免连续创建的任务全挤在同一天。
        """
        if frequency == SCHEDULE_FREQUENCY_EVERY_2_DAYS:
            return [f"{minute} {hour} */2 * *"]
        if frequency == SCHEDULE_FREQUENCY_EVERY_3_DAYS:
            return [f"{minute} {hour} */3 * *"]
        if frequency == SCHEDULE_FREQUENCY_WEEKLY:
            first = (start.weekday() + 1) % 7
            return [
                f"{minute} {hour} * * {(first + offset) % 7}"
                for offset in range(7)
            ]
        return [f"{minute} {hour} * * *"]

    def find_free_slot(
        self,
        busy_slots: Sequence[BusySlot],
        now: Optional[datetime] = None,
        frequency: str = DEFAULT_SCHEDULE_FREQUENCY,
    ) -> Optional[str]:
        """兜底：在低峰时段里挑一个不冲突、且符合频率要求的时间点。"""
        freq = normalize_frequency(frequency)
        start = now or datetime.now(self.timezone)
        lookahead_hours = FREQUENCY_LOOKAHEAD_HOURS[freq]
        for hour in FALLBACK_HOURS:
            for minute in FALLBACK_MINUTES:
                for cron in self._fallback_crons(freq, start, minute, hour):
                    fires = self.collect_fire_times(
                        cron, start, lookahead_hours=lookahead_hours
                    )
                    if fires and not self.find_conflicts(fires, busy_slots):
                        return cron
        return None

    # ------------------------------------------------------------------
    # AI 调用
    # ------------------------------------------------------------------
    def build_prompt(
        self,
        task_name: str,
        keyword: str,
        schedules: Sequence[ExistingSchedule],
        busy_slots: Sequence[BusySlot],
        preferred: str = "",
        feedback: str = "",
        frequency: str = DEFAULT_SCHEDULE_FREQUENCY,
        lookahead_hours: int = LOOKAHEAD_HOURS,
    ) -> str:
        freq = normalize_frequency(frequency)
        lines: List[str] = []
        lines.append("你是闲鱼监控机器人的调度助手。")
        lines.append(
            "每个定时任务到点都会启动爬虫抓取数据。多个任务同一时刻启动会互相争抢"
            "浏览器登录态、账号和网络带宽，也更容易触发平台风控。"
        )
        lines.append("请为新任务挑一个**不与已有任务冲突**的执行时间。")
        lines.append("")

        lines.append(f"【已有任务及其定时规则】（时区 {SCHEDULE_TIMEZONE}）")
        if schedules:
            for index, schedule in enumerate(schedules, start=1):
                first = schedule.fire_times[0].strftime("%m-%d %H:%M")
                lines.append(
                    f"{index}. {schedule.describe()}，最近一次触发 {first}"
                )
        else:
            lines.append("（暂无其它任务占用定时规则）")
        lines.append("")

        lines.append(
            f"【未来 {lookahead_hours} 小时内已被占用的触发时刻】（日期 时间）"
        )
        if busy_slots:
            # 按任务分组展示：高频任务（例如 */30）会占满整个列表，
            # 直接平铺会把其它任务的时段挤出提示词。
            for schedule in schedules:
                times = schedule.fire_times
                for fire_time in times[:PER_TASK_SLOT_SAMPLES]:
                    lines.append(
                        f"- {fire_time.strftime('%m-%d %H:%M')}（{schedule.task_name}）"
                    )
                if len(times) > PER_TASK_SLOT_SAMPLES:
                    lines.append(
                        f"- …（{schedule.task_name} 在本时段内还有 "
                        f"{len(times) - PER_TASK_SLOT_SAMPLES} 次触发）"
                    )
        else:
            lines.append("（无）")
        lines.append("")

        lines.append("【新任务信息】")
        lines.append(f"- 任务名称：{task_name or '未填写'}")
        lines.append(f"- 搜索关键词：{keyword or '未填写'}")
        if preferred:
            lines.append(f"- 用户偏好：{preferred}")
        lines.append("")

        lines.append("【要求】")
        lines.append(
            "1. 只输出一个 5 段 cron 表达式（分 时 日 月 周），不要输出任何解释文字。"
        )
        lines.append(
            f"2. 该表达式在未来 {lookahead_hours} 小时内的所有触发时刻，必须与上面的"
            f"「已被占用的触发时刻」错开 {CONFLICT_WINDOW_MINUTES} 分钟以上。"
        )
        lines.append(
            f"3. 执行频率必须为「{FREQUENCY_LABELS[freq]}」："
            f"{FREQUENCY_CRON_HINTS[freq]}。"
        )
        lines.append("4. 优先选择业务低峰时段（凌晨 1 点到 5 点）。")
        lines.append(
            "5. 刻意避开整点，选择像 03:17 这样不常见的分钟，降低撞点概率。"
        )
        lines.append(f"6. 输出示例：{FREQUENCY_CRON_EXAMPLES[freq]}")
        if feedback:
            lines.append("")
            lines.append("【上一次尝试的反馈，请务必修正】")
            lines.append(feedback)
        return "\n".join(lines)

    async def request_ai(self, prompt: str) -> str:
        """调用 AI 生成候选 cron 表达式。"""
        ai_client = self._ai_client
        owns_client = False
        if ai_client is None:
            ai_client = AIClient()
            owns_client = True

        try:
            if not ai_client.is_available():
                ai_client.refresh()
            if not ai_client.is_available():
                raise ScheduleSuggestionError(
                    "AI 客户端未初始化，无法生成定时规则。请检查 .env 中的 AI 配置。"
                )
            return await ai_client._call_ai(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_output_tokens=SCHEDULE_MAX_OUTPUT_TOKENS,
                enable_json_output=False,
            )
        finally:
            if owns_client:
                try:
                    await ai_client.close()
                except Exception as exc:  # pragma: no cover - 关闭失败不影响主流程
                    print(f"关闭 AI 客户端时出错: {exc}")

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    @staticmethod
    def parse_suggestion(text: str) -> Tuple[Optional[str], str]:
        """从 AI 回复里解析出 (cron, 说明)。解析不出 cron 时返回 (None, "")。"""
        if not text:
            return None, ""

        reason = ""
        cron: Optional[str] = None

        # 优先按 JSON 解析（模型可能额外返回 reason 字段）。
        try:
            payload = parse_ai_response_json(text)
            if isinstance(payload, dict):
                raw_cron = payload.get("cron") or payload.get("expression")
                if raw_cron:
                    cron = str(raw_cron).strip()
                raw_reason = payload.get("reason") or payload.get("explanation")
                if raw_reason:
                    reason = str(raw_reason).strip()
        except Exception:
            cron = None

        if not cron:
            cron = ScheduleSuggestionService.extract_cron_from_text(text)

        return cron, reason

    @staticmethod
    def extract_cron_from_text(text: str) -> Optional[str]:
        """从任意文本里捞出第一个形似 cron 的表达式。"""
        for raw_line in text.splitlines():
            line = raw_line.strip().strip("`").strip()
            if not line:
                continue
            line = _CRON_PREFIX_RE.sub("", line).strip()
            match = _CRON_SEARCH_RE.search(line)
            if not match:
                continue
            candidate = " ".join(match.group(1).split())
            if candidate:
                return candidate
        return None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def suggest(
        self,
        tasks: Sequence[Task],
        task_name: str = "",
        keyword: str = "",
        exclude_task_id: Optional[int] = None,
        preferred: str = "",
        frequency: str = DEFAULT_SCHEDULE_FREQUENCY,
        now: Optional[datetime] = None,
    ) -> ScheduleSuggestion:
        """为任务生成一个避开其它任务的 cron 表达式。"""
        freq = normalize_frequency(frequency)
        lookahead_hours = FREQUENCY_LOOKAHEAD_HOURS[freq]
        start = now or datetime.now(self.timezone)
        schedules = self.collect_existing_schedules(
            tasks,
            exclude_task_id=exclude_task_id,
            now=start,
            lookahead_hours=lookahead_hours,
        )
        busy_slots = self._busy_slots(schedules)
        avoided = [schedule.describe() for schedule in schedules]

        feedback = ""
        ai_error: Optional[str] = None

        for attempt in range(1, MAX_AI_ATTEMPTS + 1):
            prompt = self.build_prompt(
                task_name=task_name,
                keyword=keyword,
                schedules=schedules,
                busy_slots=busy_slots,
                preferred=preferred,
                feedback=feedback,
                frequency=freq,
                lookahead_hours=lookahead_hours,
            )
            try:
                text = await self.request_ai(prompt)
            except ScheduleSuggestionError:
                raise
            except Exception as exc:
                ai_error = str(exc)
                print(f"AI 生成定时规则失败: {exc}")
                break

            cron, reason = self.parse_suggestion(text)
            if not cron:
                feedback = (
                    "上一次回复里没有找到合法的 cron 表达式。"
                    f"请只输出一行 cron 表达式，例如：{FREQUENCY_CRON_EXAMPLES[freq]}"
                )
                continue

            normalized = normalize_cron_expression(cron)
            try:
                build_cron_trigger(normalized, timezone=self.timezone)
            except ValueError:
                feedback = (
                    f"cron 表达式 “{cron}” 无法被解析。"
                    "请给出合法的 5 段 cron（分 时 日 月 周），"
                    f"例如：{FREQUENCY_CRON_EXAMPLES[freq]}"
                )
                continue

            # 频率是硬性要求：AI 经常忽略它直接给「每天一次」，这里拦下来
            # 把期望的字段形态反馈回去让它重写。
            if not cron_matches_frequency(normalized, freq):
                feedback = (
                    f"cron 表达式 “{cron}” 不符合要求的执行频率"
                    f"（{FREQUENCY_LABELS[freq]}）。"
                    f"请按下面的格式重新给出：{FREQUENCY_CRON_HINTS[freq]}，"
                    f"例如：{FREQUENCY_CRON_EXAMPLES[freq]}。"
                )
                continue

            fires = self.collect_fire_times(
                normalized, start, lookahead_hours=lookahead_hours
            )
            if not fires:
                feedback = (
                    f"cron 表达式 “{cron}” 在未来 {lookahead_hours} 小时内不会触发，"
                    "请换一个会正常执行的表达式。"
                )
                continue

            conflicts = self.find_conflicts(fires, busy_slots)
            if conflicts:
                conflict_text = "、".join(
                    f"{slot.when.strftime('%m-%d %H:%M')}（{slot.task_name}）"
                    for _, slot in conflicts[:8]
                )
                feedback = (
                    f"cron 表达式 “{cron}” 会与以下时间冲突：{conflict_text}。"
                    f"请避开这些时刻，重新给出一个 cron 表达式。"
                )
                continue

            return ScheduleSuggestion(
                cron=normalized,
                reason=reason
                or (
                    f"AI 已避开 {len(schedules)} 个已有任务的执行时间。"
                    if schedules
                    else "当前没有其它任务占用定时规则，AI 选择了一个低峰时段。"
                ),
                source="ai",
                avoided=avoided,
                considered_tasks=len(schedules),
            )

        # 兜底：AI 不可用或始终给不出可用结果时，用确定性算法挑一个空闲时段。
        fallback_cron = self.find_free_slot(busy_slots, start, frequency=freq)
        if not fallback_cron:
            raise ScheduleSuggestionError(
                "当前已有任务的定时规则过于密集，无法在低峰时段找到空闲时间，"
                "请手动指定定时规则。"
            )

        detail = f"（AI 调用失败：{ai_error}）" if ai_error else ""
        return ScheduleSuggestion(
            cron=fallback_cron,
            reason=(
                f"AI 未能给出可用方案{detail}，"
                f"已自动选取符合「{FREQUENCY_LABELS[freq]}」的低峰时段空闲时间点。"
            ),
            source="fallback",
            avoided=avoided,
            considered_tasks=len(schedules),
        )
