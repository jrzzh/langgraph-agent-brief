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
| `search` | 并发联网检索，失败重试 3 次并退避 |
| `fetch` | 并发抓取网页正文，单页失败降级跳过 |
| `verify` | LLM 交叉核验关键事实，标注来源等级 |
| `brief` | LLM 生成简报，含字数自检与压缩重试 |

工具集（`@tool` 装饰器）：联网检索、网页正文读取、计算器。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `agent_brief.py` | 主实现：状态图流水线 + 工具定义 + 统计输出 |
| `mcp_server.py` | 本地 MCP Server（FastMCP），提供 3 个文本分析工具 |
| `mcp_demo.py` | 经 `langchain-mcp-adapters` 把 MCP 工具接入 LangGraph |
| `smoke_test.py` | 环境导入 / 模型连通性 / 检索可用性冒烟测试 |
| `generate_fill_form.py` | 生成 11 维度实测填表数据（xlsx） |
| `logs/` | 运行日志 |
| `output/` | 技术简报与统计 JSON |

## 技术栈

| 项 | 值 |
| --- | --- |
| 框架 | LangGraph 1.2.16 + LangChain 1.4.6 |
| 模型 | DeepSeek（OpenAI 兼容接口） |
| MCP | langchain-mcp-adapters 0.3.2（streamable-http） |
| 环境管理 | uv（`pyproject.toml` + `uv.lock`） |
| Python | 3.13 |

## 快速开始

```bash
# 1) 复现环境（依据 uv.lock 精确安装）
uv sync

# 2) 配置密钥
cp .env.example .env      # Windows: copy .env.example .env
# 编辑 .env，填入自己的 DEEPSEEK_API_KEY

# 3) 运行统一任务
uv run python agent_brief.py "大模型上下文工程"

# 4) MCP 接入演示（先起 server，再用另一终端跑 client）
uv run python mcp_server.py --transport streamable-http
uv run python mcp_demo.py
```

产出：`output/brief_<RUN_ID>.md`、`output/stats_<RUN_ID>.json`、`logs/run_<RUN_ID>.log`。

## 实测要点

- **一次完整运行**：总耗时 97.20s，token 13788（LLM 4 次调用），检索得 15 条结果
- **MCP 接入**：3 个工具自动发现耗时 0.42s，并成功作为节点接入状态图
- **并发与容错**：检索 / 抓取并发执行；检索失败重试 3 次退避；抓取全部失败时如实声明
  「仅从摘要提取」，不编造内容
- **模型兼容性**：切换模型只改一个环境变量、代码零改动；但实测指标差异显著
  （同一任务下另一模型为 11.82s / 4127 token）

## 已知限制

- 检索依赖搜索引擎页面解析，受目标站点反爬与网络时延影响
- MCP 的 `stdio` 传输在受限沙箱环境下不可用（命名管道被禁），故采用 `streamable-http`
- 部分站点（如知乎）拒绝程序化读取，此时该来源会被跳过并记录
- 模型对「严格 500 字」约束遵循不足（663 字 → 压缩后 583 字）

## 说明

运行日志中的本机绝对路径已脱敏为相对路径。