"""
定时建议服务

为新任务挑选一个**不与其它任务冲突**的执行时间（cron 表达式）。

背景：多个定时任务会在同一时刻启动 Playwright 爬虫，争抢浏览器登录态、账号与
网络带宽，既拖慢彼此，也更容易触发闲鱼风控。因此新任务的触发点需要与已有任务
错开。

流程：
1. 采集所有启用任务在未来 {@see LOOKAHEAD_HOURS} 小时内的触发时刻（已占用时段）；
2. 让 AI 在这些已占用时段之外挑一个合适的 cron；
3. 对 AI 返回的表达式做「可解析 + 不冲突」双重校验，不通过就把冲突点反馈给 AI
   让它重写（最多 {@see MAX_AI_ATTEMPTS} 轮）；
4. AI 始终给不出可用结果时，退化为确定性的兜底算法：在低峰时段里挑一个空闲的
   整点偏分钟，保证功能不会因为模型不可用而彻底不可用。
"""
from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from apscheduler.util import astimezone

from src.core.cron_utils import build_cron_trigger, normalize_cron_expression
from src.domain.models.task import Task
from src.infrastructure.external.ai_client import AIClient
from src.services.ai_response_parser import parse_ai_response_json

SCHEDULE_TIMEZONE = "Asia/Shanghai"

# 两个任务的触发时刻相差在 10 分钟以内即视为冲突：爬虫本身会持续占用账号和网络，
# 仅仅"分钟数不同"并不能真正错开资源。
CONFLICT_WINDOW_MINUTES = 10

# 校验冲突时向前看的时长。
LOOKAHEAD_HOURS = 48

# 单个任务最多采样多少个触发点，避免 */1 这类高频表达式把样本撑爆。
MAX_FIRE_SAMPLES_PER_TASK = 96

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
    ) -> List[datetime]:
        """计算某个 cron 在未来 LOOKAHEAD_HOURS 小时内的全部触发时刻。"""
        normalized = normalize_cron_expression(cron)
        if not normalized:
            return []

        try:
            trigger = build_cron_trigger(normalized, timezone=self.timezone)
        except ValueError:
            return []

        deadline = start + timedelta(hours=LOOKAHEAD_HOURS)
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

            fire_times = self.collect_fire_times(task.cron, start)
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

    def find_free_slot(
        self,
        busy_slots: Sequence[BusySlot],
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """兜底：在低峰时段里挑一个不冲突的整点偏分钟。"""
        start = now or datetime.now(self.timezone)
        for hour in FALLBACK_HOURS:
            for minute in FALLBACK_MINUTES:
                cron = f"{minute} {hour} * * *"
                fires = self.collect_fire_times(cron, start)
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
    ) -> str:
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
            f"【未来 {LOOKAHEAD_HOURS} 小时内已被占用的触发时刻】（日期 时间）"
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
            f"2. 该表达式在未来 {LOOKAHEAD_HOURS} 小时内的所有触发时刻，必须与上面的"
            f"「已被占用的触发时刻」错开 {CONFLICT_WINDOW_MINUTES} 分钟以上。"
        )
        lines.append("3. 优先选择业务低峰时段（凌晨 1 点到 5 点），并保证每天执行一次。")
        lines.append(
            "4. 刻意避开整点，选择像 03:17 这样不常见的分钟，降低撞点概率。"
        )
        lines.append("5. 输出示例：17 3 * * *")
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
        now: Optional[datetime] = None,
    ) -> ScheduleSuggestion:
        """为任务生成一个避开其它任务的 cron 表达式。"""
        start = now or datetime.now(self.timezone)
        schedules = self.collect_existing_schedules(
            tasks, exclude_task_id=exclude_task_id, now=start
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
                    "请只输出一行 cron 表达式，例如：17 3 * * *"
                )
                continue

            normalized = normalize_cron_expression(cron)
            try:
                build_cron_trigger(normalized, timezone=self.timezone)
            except ValueError:
                feedback = (
                    f"cron 表达式 “{cron}” 无法被解析。"
                    "请给出合法的 5 段 cron（分 时 日 月 周），例如：17 3 * * *"
                )
                continue

            fires = self.collect_fire_times(normalized, start)
            if not fires:
                feedback = (
                    f"cron 表达式 “{cron}” 在未来 {LOOKAHEAD_HOURS} 小时内不会触发，"
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
        fallback_cron = self.find_free_slot(busy_slots, start)
        if not fallback_cron:
            raise ScheduleSuggestionError(
                "当前已有任务的定时规则过于密集，无法在低峰时段找到空闲时间，"
                "请手动指定定时规则。"
            )

        detail = f"（AI 调用失败：{ai_error}）" if ai_error else ""
        return ScheduleSuggestion(
            cron=fallback_cron,
            reason=(
                f"AI 未能给出可用方案{detail}，已自动选取低峰时段的空闲时间点。"
            ),
            source="fallback",
            avoided=avoided,
            considered_tasks=len(schedules),
        )
