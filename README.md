# LangGraph 单框架实测

基于 **LangGraph** 构建一条「联网检索 → 关键信息提取 → 事实交叉核验 → 生成技术简报」的
智能体流水线，用于实测该框架在 **Skills 封装** 与 **MCP 接入** 两项能力上的实际表现。

## 统一任务

给定一个技术主题，智能体自动完成：

```
联网检索资料 → 提取关键信息 → 事实交叉核验 → 输出 500 字以内带来源链接的 Markdown 简报
```

用 LangGraph 的 `StateGraph` 编排为 6 个节点：

```
START → plan → skill_select → search → fetch → verify → brief → END
```

| 节点 | 职责 |
| --- | --- |
| `plan` | LLM 把主题拆成 3 个检索关键词 |
| `skill_select` | 技能路由：凭技能目录选中技能并**按需加载**其正文（渐进式披露） |
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

## Skills 层（声明式技能 + 渐进式披露）

技能遵循 [Agent Skills 规范](https://agentskills.io/specification)：每个技能 = 一个目录 +
一个 `SKILL.md`（frontmatter 写元信息，正文为指令），新增技能只需写一个 Markdown 文件，
**不改代码**。

**实现采用 LangChain 官方 `deepagents` 的原生 Skills**（入口 `agent_deepagents.py`）：

```python
agent = create_deep_agent(model=llm, tools=TOOLS,
                          backend=FilesystemBackend(root_dir=BASE_DIR, virtual_mode=True),
                          skills=["/skills/"])
```

| 层级 | 加载内容 | 进入上下文时机 | 实测成本 |
| --- | --- | --- | --- |
| 1 Metadata | `name` + `description` | 启动，每个技能一行 | 5 个技能 **311 token** |
| 2 Instructions | 完整 `SKILL.md` | 判定相关后 `read_file` | 5 个正文 **1800 token** |
| 3 Resources | `references/` `scripts/` | 指令引用时按需 | 本项目未使用 |

同一批技能文件另有两种实现（详见 [`skills/README.md`](skills/README.md)）：
自建 [`skill_loader.py`](skill_loader.py)（零依赖、可计量，`agent_brief.py --skills=lazy|eager|off`）
与第三方 `langchain-agentskills`（已评估，零新增依赖）。

### 实测（同任务、同模型）

| 实现 | 耗时 | 输入 token | 技能加载 |
| --- | --- | --- | --- |
| 自建 `lazy` | 147.5 ~ 386.6 s | **4.1 k ~ 4.2 k** | 3/5（路由选中） |
| 自建 `eager` | 512.1 s | 5 126 | 5/5（启动全量） |
| **deepagents 原生** | **126.5 ~ 433.2 s** | **54.7 k ~ 111.5 k** | 5/5（模型自主读取） |

四条结论：

- **耗时上两者相当、且各自波动都很大**（实现内部波动 > 实现之间差异）：
  同为自建 `lazy`，一次 386.6 s、一次 147.5 s。主导因素是**单次调用的输出长度**
  （自建 `verify` 曾单节点 280.89 s；deepagents 第 5 步曾 390.26 s / 输出 14 847 token），
  因此**不能表述为"自建更快"**
- **真正的差异在输入 token，相差 13~27 倍**：自建每步重拼窄 prompt、历史不累积；
  deepagents 每轮重发全部历史
- **技能加载机制是本质区别**：自建用一次被强约束的路由调用稳定选出 3/5；
  deepagents 由模型自主 `read_file`，四次运行均读满 5/5
- **按需加载的收益会被模型行为抵消**：受控对照下 `lazy` 比 `eager` 省 25.5% 输入 token /
  32.5% 耗时，但在 deepagents 中因模型读满 5 个技能而**兑现为 0**

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `agent_brief.py` | 主实现：状态图流水线 + MCP 接入 + Skills 对照层 + 双后端降级 + 统计输出 |
| `agent_deepagents.py` | 统一任务的 **deepagents 原生 Skills** 实现（三级渐进式披露） |
| `skill_loader.py` | 自建 Skills 运行时（对照实现）：注册表 + 按需加载 + 计量 |
| `file_tools.py` | workspace 文件读写工具（默认关闭）：路径沙箱 + 类型白名单 + 原子写 |
| `context_compression.py` | 对话历史压缩：超窗口 75% 时折成「摘要 + 最近 6 轮」，增量维护 |
| `skills/` | 技能单元目录（每个技能一个 `SKILL.md`，符合 Agent Skills 规范） |
| `smoke_test.py` | 环境导入 / 模型连通性 / 检索可用性冒烟测试 |
| `logs/` | 运行日志 |
| `output/` | 技术简报与统计 JSON |

## 技术栈

| 项 | 值 |
| --- | --- |
| 框架 | LangGraph 1.2.16 + LangChain 1.4.7（langchain-core 1.7.0） |
| 模型 | DeepSeek（OpenAI 兼容接口，`deepseek-flash`） |
| MCP | langchain-mcp-adapters 0.3.2（Streamable HTTP） |
| Skills | deepagents 0.7.32（Agent Skills 规范，三级渐进式披露） |
| 环境管理 | uv（`pyproject.toml` + `uv.lock`） |
| Python | 3.13 |

## 组员上手（从零跑通）

**前置条件**：Git、[uv](https://docs.astral.sh/uv/)（Python 由 uv 托管，不必单独安装）。

```powershell
git clone https://github.com/agent-hdu-team/langgraph-agent-brief.git
cd langgraph-agent-brief
uv sync                                  # 按 uv.lock 精确安装
Copy-Item .env.example .env               # 再编辑 .env 填入自己的 Key
uv run python agent_brief.py "大模型上下文工程"
```

只需要两个 Key，都填在 `.env` 里（该文件已被 gitignore，不会提交）：

| 变量 | 用途 | 缺失时 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 模型调用 | 401，跑不了 |
| `DASHSCOPE_API_KEY` | 千问 MCP（检索 / 抓取） | **不影响跑通**，自动降级到 Bing + requests |

下面「快速开始」是同一套命令的简版，供已经拿到代码时参考。

## 快速开始

```bash
# 1) 复现环境（依据 uv.lock 精确安装）
uv sync

# 2) 配置密钥
cp .env.example .env      # Windows: copy .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 与 DASHSCOPE_API_KEY

# 3) 运行统一任务（本项目的两套实现，任选其一）
uv run python agent_deepagents.py "大模型上下文工程"                  # ② 官方 deepagents 原生 Skills
uv run python agent_brief.py "大模型上下文工程"                       # ① 自建 Skills 层，默认 --skills=lazy
uv run python agent_brief.py "大模型上下文工程" --skills=eager        # ① 对照：启动时全量加载技能
uv run python agent_brief.py "大模型上下文工程" --skills=off          # ① 基线：不使用技能

# 4) 图形界面（Skills 实现二选一：自建 skill_loader / 官方 deepagents，可看运行日志）
uv run streamlit run streamlit_app.py     # → http://localhost:8501
start-ui.cmd                              # 或双击启动（Windows）
```

产出：

| 实现 | 简报 | 统计 | 日志 |
| --- | --- | --- | --- |
| `agent_brief.py` | `output/brief_<RUN_ID>.md` | `output/stats_<RUN_ID>.json` | `logs/run_<RUN_ID>.log` |
| `agent_deepagents.py` | `output/brief_deepagents_<RUN_ID>.md` | `output/stats_deepagents_<RUN_ID>.json` | `logs/deepagents_<RUN_ID>.log` |

> 未配置 `DASHSCOPE_API_KEY` 也能跑通：工具会自动降级为本地实现（Bing 检索 / requests+lxml 抓取）。

## 接入通用 UI（agent-ui）

本项目除了自带命令行与 Streamlit 界面，还导出了 `build_llm()` / `build_agent()` 两个工厂函数，
可以被通用 Agent UI 当作后端加载。完整的适配器在 `agent-ui` 仓库的 `langgraph接入` 分支上。

```powershell
git clone https://github.com/agent-hdu-team/agent-ui.git
cd agent-ui
git checkout langgraph接入
uv sync

$env:LANGGRAPH_BRIEF_HOME = "<本项目的绝对路径，每人不同>"
$env:AGENT_UI_BACKEND = "langgraph_brief"
uv run agent-ui
```

启动后在界面左侧模型面板新建一个模型档案（填自己的 DeepSeek Key）并选中，即可对话。

可选环境变量：

| 变量 | 作用 |
| --- | --- |
| `LANGGRAPH_BRIEF_ENABLE_SHELL=1` | 把只读 shell 工具交给模型（默认关闭；开启后非版本查询类命令会弹审批） |
| `LANGGRAPH_BRIEF_SHOW_REASONING=1` | 保留思维链事件（默认静音） |
| `LANGGRAPH_BRIEF_SHOW_SKILL_IO=1` | 保留读取 `skills/` 的工具卡片（默认静音） |

四个必须知道的点：

1. **`LANGGRAPH_BRIEF_HOME` 是绝对路径，每人不同**，不能写死别人的目录；
   若写进 `agent-ui/.env` 是**无效的**——agent-ui 的 `.env` 只被 pydantic-settings 读 `AGENT_UI_*` 前缀。
2. **`agent-ui` 里只有 `mock` 与 `langgraph_brief` 真正可用**。`crew` / `deepagents` / `openai`
   三个适配器依赖的 vendored 包不在仓库里（被 gitignore 排除），选中会报 `ModuleNotFoundError`。
3. **我们的项目必须用同一个分支**：接 agent-ui 时若上游项目停在缺少 `build_agent()` 的旧提交上，
   会直接报错。
4. **改了代码要重启 agent-ui**：适配器是懒导入，模块一旦载入就缓存在 `sys.modules` 里，
   换分支或改文件都不会热更新。

## 常见报错

| 现象 | 原因与处理 |
| --- | --- |
| `401 Authentication Fails` | `.env` 里的 Key 没换成自己的，或复制时带了空格 |
| 检索结果很少 / 提到「已降级 Bing」 | 没配 `DASHSCOPE_API_KEY`，属预期降级 |
| `未设置 LANGGRAPH_BRIEF_HOME` | 环境变量没设，或 `setx` 后没开新终端 |
| 界面上后端名不是「LangGraph 简报」 | `AGENT_UI_BACKEND` 没生效 |
| 单次运行 80 秒以上 | 用的是推理模型 `deepseek-flash`（输出约九成是思维链）；加 `--model=deepseek-chat` 可降到十几秒 |
| `ModuleNotFoundError: crew_assistant` 等 | 选了 vendored 包缺失的后端，换 `mock` 或 `langgraph_brief` |

## 实测要点（同任务、同模型，两种实现各跑多次）

| 实现 | 耗时 | 输入 token | 技能加载 |
| --- | --- | --- | --- |
| `agent_brief.py --skills=lazy`（自建） | 147.5 ~ 386.6 s | 4.1 ~ 4.2 k | 3/5 |
| `agent_brief.py --skills=eager`（对照） | 512.1 s | 5.1 k | 5/5 |
| `agent_deepagents.py`（官方原生） | 126.5 ~ 433.2 s | 54.7 ~ 111.5 k | 5/5 |

- **耗时相当、且各自波动都很大**（实现内部波动 > 实现之间差异），主导因素是**单次调用的输出长度**，
  因此**不能表述为"自建更快"**
- **真正的架构差异在输入 token：13~27 倍** —— 自建每步重拼窄 prompt、历史不累积；
  deepagents 每轮重发全部历史
- **技能加载机制**：自建用一次被强约束的路由调用稳定选出 3/5；
  deepagents 由模型自主 `read_file`，四次运行均读满 5/5
- **MCP 接入**：一次 `get_tools()` 自动发现 2 个生产级 MCP 工具（耗时 3.19 s），并作为节点接入状态图
- **双后端降级**：MCP 失败 → 重试 1 次 → 降级本地实现；实测某次运行中 3 个并发查询瞬时全部失败，
  自动降级后任务仍完整跑完并产出简报
- **质量提升**：接入 MCP 后网页读取成功率 **0/3 → 3/3**
- **模型兼容性**：切换模型只改 `DEEPSEEK_MODEL` 一个环境变量、代码零改动
  （同一任务下另一模型为 11.82 s / 4 127 token）

> 多次运行的完整数据、受控对照（lazy vs eager）与取舍分析见 [`skills/README.md`](skills/README.md) §四。

## 已知限制

- **未配置 `.env` 时导入会直接失败**：`agent_brief.py` 在**模块级**构造模型客户端，
  缺少 `DEEPSEEK_API_KEY` 会抛 `KeyError: 'DEEPSEEK_API_KEY'`。
  请先按「快速开始」第 2 步配置 `.env`（这是预期行为，用于快速暴露配置遗漏）
- **字数口径**：500 字限制**只约束正文**，"来源"小节（链接）不计入，故保留**循环压缩**兜底，
  会额外增加 token 消耗。实测仍有超限个例：deepagents 版正文 612 字（超 22%），
  且模型对该次输出的自估字数（"约 460 字"）**偏低约 25%**
- MCP 的 `stdio` 传输在受限沙箱环境下不可用（命名管道被禁），故采用 `streamable-http`
- 远程 MCP 服务依赖网络；不可用时自动降级本地实现（检索质量与反爬能力会下降）
- 部分站点（如知乎）拒绝程序化读取，此时该来源会被跳过并记录
- 技能层 token 计量依赖 `tiktoken`，**首次使用需联网下载约 1.7 MB 词表**（实测 28 s，
  之后缓存命中约 165 ms）；离线环境可设 `SKILL_TOKEN_ESTIMATOR=heuristic` 免网络，
  但两种估算器口径不同，横向比较时不可混用。词表缓存已固定到项目内 `.cache/tiktoken`（不入库）
