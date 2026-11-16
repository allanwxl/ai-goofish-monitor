# MCP 数据服务最小可用实现 · 实施记录

> 分支：`feat/mcp-data-service`（从 `master` 切出）
> 实施日期：2026-11-10
> 关联文档：[`商业化可行性评估.md`](./商业化可行性评估.md)

---

## 1. 交付目标

在 `ai-goofish-monitor` 中实现最小可用的 MCP 服务，实现"提供 MCP 接口提供数据服务"的最小闭环：

1. `.env` 配置 API Key / Secret
2. 可被外部 Agent 调用的 MCP 服务
3. 分页查询监控结果

---

## 2. 实施步骤与结果

| # | 步骤 | 结果 |
|---|---|---|
| 0 | 从 `master` 切出 `feat/mcp-data-service` | ✅ 工作区原本干净，起点 `e4ad58e` |
| 1 | 建目录 + 装 `fastmcp` | ✅ `src/mcp/`、`docs/`；锁定 `fastmcp==4.1.7`（MCP Python SDK 2.3.0） |
| 2 | 配置层 | ✅ `settings.py` 新增 `MCPSettings`；`.env.example` 新增第 7 节 |
| 3 | 鉴权模块 `src/mcp/auth.py` | ✅ API Key 解析与校验 |
| 4 | 工具模块 `src/mcp/tools.py` | ✅ 4 个只读工具 + 白名单裁剪 |
| 5 | 服务组装 `src/mcp/server.py` | ✅ FastMCP 实例 + 鉴权 + `/health` |
| 6 | 启动入口 `src/mcp_server.py` | ✅ `python -m src.mcp_server` |
| 7 | 单元测试 | ✅ `tests/unit/test_mcp_server.py`，25 个用例全绿 |
| 8 | 文档 | ✅ 本文件 + `商业化可行性评估.md` |
| 9 | 端到端验证 | ✅ 10 项实测全部通过（见 §5） |

---

## 3. 变更清单

### 新增

| 文件 | 行数 | 职责 |
|---|---|---|
| `src/mcp/__init__.py` | 8 | 包声明 |
| `src/mcp/auth.py` | ~110 | API Key 解析（`key` / `key:tenant`）、常量时间校验、token map 构建 |
| `src/mcp/tools.py` | ~330 | 4 个只读工具、白名单裁剪、参数校验 |
| `src/mcp/server.py` | ~95 | FastMCP 组装、鉴权挂载、`/health`、启动日志 |
| `src/mcp_server.py` | ~55 | 独立启动入口 |
| `tests/unit/test_mcp_server.py` | ~430 | 25 个单元测试 |
| `docs/商业化可行性评估.md` | — | 评估文档 |
| `docs/plan.md` | — | 本文件 |

### 修改（均为追加式，无既有行删除）

| 文件 | 变更 |
|---|---|
| `src/infrastructure/config/settings.py` | 新增 `MCPSettings` 类 + `parsed_api_keys()` / `parsed_allowed_tools()`；`reload_settings()` 同步重建 `mcp_settings`；末尾导出 `mcp_settings` |
| `requirements.txt` | 追加 `fastmcp==4.1.7` |
| `requirements-runtime.txt` | 追加 `fastmcp==4.1.7` |
| `.env.example` | 追加第 7 节「MCP 数据服务配置」 |

**未改动**：`src/scraper.py`、`src/ai_handler.py`、`src/app.py`、`src/api/**`、`src/services/**`、`src/infrastructure/persistence/**`、`spider_v2.py`、`web-ui/**`、Dockerfile、compose 文件。

---

## 4. 与原计划的偏差

实施中发现并修正了 4 处，其中 3 处是计划的事实性疏漏，1 处是原设计的正确性缺陷。**所有偏差均为机械性修正，未改变计划的任何决策**（仍是：独立进程、FastMCP 官方鉴权、只读工具集、白名单裁剪、无多租户 schema 变更）。

### 偏差 1：价格历史服务是同步函数，不是 async

**计划原文**：「这些函数是 `async` 且内部用 `asyncio.to_thread` 包了同步 SQLite 操作。MCP tool 直接 `await` 它们即可，**不要**在 tool 里再包 `to_thread`（会重复）。」

**实际情况**：`list_result_filenames` / `query_result_records` 确实是 async，但 `load_price_snapshots` / `build_price_history_insights` 是**同步函数**（`src/services/price_history_service.py:159,364`）。

**处理**：async 函数直接 await，同步函数包 `asyncio.to_thread`。修正了计划中"全部是 async"的错误判断，避免了同步 SQLite 调用阻塞事件循环。

### 偏差 2：`FastMCP.list_tools()` 是协程，不是同步方法

**计划原文**：未提及，但 `describe_server()` 最初按同步方法使用。

**实际情况**：FastMCP 4.1.7 的 `list_tools()` 返回协程。最初实现会导致**服务启动即崩溃**（`TypeError: 'coroutine' object is not iterable`）。单元测试 `test_allowed_tools_whitelist_restricts_exposure` 捕获了该问题。

**处理**：`describe_server()` 改用 `TOOL_NAMES` 常量 + 白名单过滤计算，不依赖协程；测试中改用 `asyncio.run(server.list_tools())`。

### 偏差 3：API Key 需支持 `key:tenant` 完整串

**计划原文**：「`verify_api_key(authorization_header) -> str | None`：提取 Bearer、比对、返回租户标识」。

**实际情况**：原实现只用 `entry.key`（已剥离租户部分）做比对，导致客户端传 `.env` 中的完整串 `sk-tenant-key:tenant-x` 时**匹配失败**。单元测试 `test_verify_api_key_extracts_tenant` 捕获了该问题。

**处理**：
- `verify_api_key()` 先按纯 key 匹配，再按 `key:tenant` 完整串匹配
- `build_token_map()` 同时把纯 key 与完整串登记为有效 token，确保 FastMCP 的 `StaticTokenVerifier` 两种形式都能放行

### 偏差 4：`StaticTokenVerifier` 的实际行为已实测确认

计划要求"实施第一步先跑探针验证实际安装版本的 FastMCP API 签名"。已按此执行，探针结论：

| 探针项 | 结论 |
|---|---|
| `StaticTokenVerifier` 位置 | `fastmcp.server.auth.providers.jwt` ✅ 与计划一致 |
| 签名 | `(tokens: dict[str, dict[str, Any]], required_scopes: list[str] \| None = None)` |
| 是否强制 JWT 结构 | **否** —— 纯字符串字典查找，官方文档明确支持 "simple API key-style authentication" |
| `transport` 枚举 | `"http"` / `"stdio"` / `"sse"` / `"streamable-http"` 均可用；实现选 `"http"` + `path="/mcp"` |
| `@mcp.custom_route` | 可用，`/health` 成功挂载（路由表 `['/mcp', '/health']`） |
| `fastmcp.middleware` | **路径错误** —— 实际为 `fastmcp.server.middleware`（计划中的备选路径需修正） |
| 鉴权响应 | 无 Key / 错 Key → 401 + `WWW-Authenticate: Bearer` + `{"error":"invalid_token"}`；正确 Key → 200 |

**采用方案**：计划首选的 `StaticTokenVerifier`（`auth=` 参数），与计划一致。同时实测了备选的 ASGI 拦截方案，行为等价但需手写中间件，故不采用。

---

## 5. 验收结果

### 5.1 端到端实测（真实启动 `python -m src.mcp_server` 子进程 + 官方 MCP 客户端）

| # | 验收项 | 结果 |
|---|---|---|
| 1 | MCP 服务可启动，健康检查返回 200 | ✅ |
| 2 | Key 缺失被拒 | ✅ 401 |
| 3 | Key 错误被拒 | ✅ 401 |
| 4 | Key 正确放行，列出 4 个工具 | ✅ |
| 5 | 空 Key 配置拒绝启动 | ✅ `MCPConfigurationError` |
| 6 | `list_all_results` 返回结果集清单 | ✅ 1 个结果集 / 14 件商品 |
| 7 | `search_items` 分页正确 | ✅ page1/page2 商品不重叠，`total_items` 均为 14 |
| 8 | 推荐筛选互斥报错 | ✅ 「AI推荐筛选与关键词推荐筛选不能同时开启。」 |
| 9 | 字段裁剪生效 | ✅ 结构无敏感键，真实昵称值未出现 |
| 10 | 无数据不报错 | ✅ 返回空集 |
| 11 | 既有功能不回归 | ✅ 见 §5.3 |
| 12 | 密钥不入库不入仓 | ✅ 见 §6 |

补充验证：`limit=101` 被拒（「limit 必须在 1 到 100 之间」）；`get_market_insights` 返回真实行情（均价 ¥4048.47 / 中位 ¥4008.5）。

### 5.2 单元测试

`tests/unit/test_mcp_server.py` —— **25 passed**，覆盖：

- 鉴权：Key 解析变体、纯 key / `key:tenant` / Bearer 前缀 / 错 Key / 缺 Key / 空配置拒绝启动
- 裁剪：结构化断言 + 真实昵称值断言 + 非字典输入
- 分页：page=1 vs page=2 不重叠、`limit` 上下界、`page=0`、非法 `sort_by`
- 行为：推荐筛选互斥、未知关键词返回空集、真实查询路径已脱敏
- MCP 协议层：真实 HTTP 下正确 Key 放行 / 错 Key / 无 Key 被拒；`/health` 路由；白名单限制工具暴露；库未初始化返回可读错误；**MCP 调用不写库**

### 5.3 全量测试

```
2 failed, 197 passed, 3 skipped
```

2 个失败为 `tests/unit/test_failure_guard_timezone.py`，**经 `git worktree` 在 `master` 基线复现确认为既有失败**（最近提交 `e4ad58e fix(failure-guard): 自动恢复比对错用登录态文件导致永不触发` 引入），与本次改动无关。

### 5.4 依赖兼容性

安装 `fastmcp==4.1.7` 后核心依赖**零降级**：

```
pydantic  2.14.3      (不变)
fastapi   0.162.0     (不变)
openai    3.46.0      (不变)
starlette 1.10.0      (不变)
```

---

## 6. 密钥管理核对

- `.env` 未被提交（`.gitignore` 已含 `.env`）
- 仓库内无任何 API Key 明文
- `MCP_API_KEYS` 仅从环境变量读取，不写入数据库、不硬编码
- 启动日志只打印 **Key 数量**，不打印 Key 明文
- `host=0.0.0.0` 时启动日志打印警告，提示必须在反向代理层启用 HTTPS

---

## 7. 使用说明

### 7.1 配置 `.env`

```bash
MCP_ENABLED=true
MCP_HOST=127.0.0.1          # 对外服务时改 0.0.0.0，务必配反向代理 HTTPS
MCP_PORT=8001
MCP_API_KEYS=sk-abc123,sk-def456:tenant-b
MCP_ALLOWED_TOOLS=          # 留空 = 全部开放
```

### 7.2 启动

```bash
python -m src.mcp_server
```

### 7.3 客户端连接

```json
{
  "mcpServers": {
    "xianyu-monitor": {
      "url": "http://127.0.0.1:8001/mcp",
      "headers": { "Authorization": "Bearer sk-abc123" }
    }
  }
}
```

客户端可传纯 key，也可传 `.env` 中的完整 `key:tenant` 形式。

---

## 8. 已知技术债（下一阶段处理）

| 项 | 说明 | 处理阶段 |
|---|---|---|
| 多 Key 无隔离 | 所有 Key 等权，可见同一份数据。API Key 目前只是访问控制，不是租户隔离 | P3（加 `tenant_id`） |
| 无审计日志 | 工具调用不留痕 | P4 |
| 无限流 | Agent 可能高频调用，与爬虫抢 IO | P4 |
| 无 TLS | `0.0.0.0` 时 Key 明文过网，需反向代理 | 部署侧 |
| 无建任务能力 | MCP 只能读，不能写 | P3 |
| 无合规审核 | 关键词可任意下发（当前 MCP 不支持下发，故风险尚未暴露） | P1 |
| 无计量计费 | 无配额、无账单 | P3 |

---

## 9. 后续阶段

| 阶段 | 内容 | 退出条件 |
|---|---|---|
| P1 | 数据覆盖扩广（30+ 关键词）+ 合规审核服务 + 多租户 schema（不对外开放） | 覆盖 ≥30 关键词；月 AI 成本 ≤ $120 |
| P2 | 邀请制免费额度（不可建任务）+ 计量日志 | ≥20 名开发者接入；周活跃调用 ≥100 |
| P3 | 开放建任务（月度配额制）+ 订阅结算 + 成本监控告警 | ≥5 个付费用户；单位关键词月成本 ≤ $4 |
| P4 | OAuth 2.1 + 审计日志 + 合规加固 | — |

> **风险时间窗**：P2 的邀请制阶段是最后一个低风险验证窗口。进入 P3 开放建任务并计费后，风险性质改变 —— 规模扩大只会放大风险，不会降低风险。
