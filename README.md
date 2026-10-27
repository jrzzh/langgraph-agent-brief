# LangGraph 单框架实测

基于 **LangGraph** 构建一条「联网检索 → 关键信息提取 → 事实交叉核验 → 生成技术简报」的
智能体流水线，用于实测该框架在 **Skills 封装** 与 **MCP 接入** 两项能力上的实际表现。

## 统一任务

给定一个技术主题，智能体自动完成：

```
联网检索资料 → 提取关键信息 → 事实交叉核验 → 输出 500 字以内带来源链接的 Markdown 简报
```

用 LangGraph 的 `StateGraph` 编排为 5 个节点：

```
START → plan → search → fetch → verify → brief → END
```

| 节点 | 职责 |
| --- | --- |
| `plan` | LLM 把主题拆成 3 个检索关键词 |
| `search` | 并发联网检索（MCP 主 + Bing 降级） |
| `fetch` | 并发抓取网页正文（MCP 主 + 本地降级） |
| `verify` | LLM 交叉核验关键事实，标注来源等级 |
| `brief` | LLM 生成简报，含字数自检与循环压缩 |

## 工具集与 MCP 接入

三个工具均由 `@tool` 装饰器定义（即 LangGraph 的「工具即节点函数」），前两个**走 MCP**：

| 工具 | 主后端（MCP） | 降级后端 |
| --- | --- | --- |
| `web_search` | 千问 **EnhancedSearch** MCP → `search_pro` | Bing 页面解析 |
| `web_fetch` | 千问 **WebFetch** MCP → `web_fetch` | requests + lxml |
| `calculator` | 本地实现（ast 安全求值） | — |

MCP 经 `langchain-mcp-adapters` 的 `MultiServerMCPClient`，以 **Streamable HTTP** 接入：

```python
client = MultiServerMCPClient({
    "qwen-search": {"transport": "streamable_http",
                    "url": "https://maas.qianwenaiapi.com/api/v1/mcps/EnhancedSearch/mcp",
                    "headers": {"Authorization": f"Bearer {key}"}},
    "qwen-fetch":  {"transport": "streamable_http",
                    "url": "https://maas.qianwenaiapi.com/api/v1/mcps/WebFetch/mcp",
                    "headers": {"Authorization": f"Bearer {key}"}},
})
tools = await client.get_tools()      # 自动发现: search_pro / web_fetch
```

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `agent_brief.py` | 主实现：状态图流水线 + MCP 接入 + 双后端降级 + 统计输出 |
| `mcp_server.py` | 本地 MCP Server（FastMCP），示教用 |
| `mcp_demo.py` | 经 `langchain-mcp-adapters` 把 MCP 工具接入 LangGraph |
| `smoke_test.py` | 环境导入 / 模型连通性 / 检索可用性冒烟测试 |
| `logs/` | 运行日志 |
| `output/` | 技术简报与统计 JSON |

## 技术栈

| 项 | 值 |
| --- | --- |
| 框架 | LangGraph 1.2.16 + LangChain 1.4.6 |
| 模型 | DeepSeek（OpenAI 兼容接口，`deepseek-v4-pro`） |
| MCP | langchain-mcp-adapters 0.3.2（Streamable HTTP） |
| 环境管理 | uv（`pyproject.toml` + `uv.lock`） |
| Python | 3.13 |

## 快速开始

```bash
# 1) 复现环境（依据 uv.lock 精确安装）
uv sync

# 2) 配置密钥
cp .env.example .env      # Windows: copy .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 与 DASHSCOPE_API_KEY

# 3) 运行统一任务
uv run python agent_brief.py "大模型上下文工程"

# 4) 本地 MCP Server 示教（先起 server，再用另一终端跑 client）
uv run python mcp_server.py --transport streamable-http
uv run python mcp_demo.py
```

产出：`output/brief_<RUN_ID>.md`、`output/stats_<RUN_ID>.json`、`logs/run_<RUN_ID>.log`。

## 实测要点

- **一次完整运行**：总耗时 158.61s，token 11481（LLM 3 次调用），检索得 15 条结果，**零故障记录**
- **MCP 接入**：一次 `get_tools()` 自动发现 2 个生产级 MCP 工具（耗时 3.19s），并作为节点接入状态图
- **双后端降级**：MCP 失败 → 重试 1 次 → 降级本地实现。实测有效：某次运行中 MCP 的 3 个并发查询瞬时
  全部失败，自动降级后任务完整跑完并产出简报
- **质量提升**：接入 MCP 后网页读取成功率 **0/3 → 3/3**，简报正文 **338 字达标**
  （500 字限制**只约束正文**，"来源"小节不计入）
- **模型兼容性**：切换模型只改一个环境变量、代码零改动；但实测指标差异显著
  （同一任务下另一模型为 11.82s / 4127 token）

## 已知限制

- **字数口径**：500 字限制**只约束正文**，"来源"小节（链接）不计入；模型偶有超长（实测 1 次 613 字），
  故保留**循环压缩**兜底，会额外增加 token 消耗
- MCP 的 `stdio` 传输在受限沙箱环境下不可用（命名管道被禁），故采用 `streamable-http`
- 远程 MCP 服务依赖网络；不可用时自动降级本地实现（检索质量与反爬能力会下降）
- 部分站点（如知乎）拒绝程序化读取，此时该来源会被跳过并记录
