# Skills —— 声明式技能单元与三种实现方式

本目录对应开题报告的 **研究问题 1**（各框架如何封装可复用技能）与 **研究问题 3**
（工具/技能的加载时机：启动全量 vs 运行时按需），并为单框架维度 **2**、**4** 提供实测数据。

> **每个技能 = 一个目录 + 一个 `SKILL.md`**。
> 目录结构、frontmatter 字段均遵循 [Agent Skills 规范](https://agentskills.io/specification)，
> 因此同一批技能文件可被本项目三种实现共用，也可被其他支持该规范的 agent 直接复用。

## 一、技能单元规范

```
skills/
└── <skill-name>/
    ├── SKILL.md        # frontmatter（元信息）+ Markdown 正文（指令）
    ├── references/     # 可选：按需读取的参考资料
    ├── scripts/        # 可选：可执行脚本
    └── assets/         # 可选：模板/图片等静态资源
```

`SKILL.md` 的 frontmatter（已按规范收敛为 `name` / `description` / `metadata`）：

```markdown
---
name: search-strategy                     # 必填，必须与父目录同名
description: 为技术主题设计高命中率检索关键词：中英双语、控制词长、去泛化词  # 必填，≤1024 字符
metadata:                                 # 规范允许的任意键值对
  version: "1.0"
  tags: [search, retrieval]
  priority: high
---
# 检索关键词设计
（技能正文 —— 只有被激活时才进入上下文）
```

**新增技能的成本**：建一个目录 + 写一个 Markdown 文件，**不改任何 Python 代码、不重新打包**。

## 二、渐进式披露（Progressive Disclosure）

| 层级 | 加载内容 | 进入上下文时机 | 实测成本 |
| --- | --- | --- | --- |
| 1 Metadata | `name` + `description` | 启动即注入 system prompt（每个技能一行） | 5 个技能共 **311 token** |
| 2 Instructions | 完整 `SKILL.md` 正文 | 判定相关后加载（`read_file` 或路由注入） | 5 个正文共 **1800 token** |
| 3 Resources | `references/` `scripts/` `assets/` | 指令引用时按需读取 | 本项目未使用 |

关键性质：**技能数量增长时，常驻成本只随"目录"增长，而不随"正文"增长**。

## 三、三种实现方式（本项目均已评估）

### ① 自建 `skill_loader.py`（保留，作为可计量的对照实现）

约定式实现，约 200 行，零新增依赖，可在裸 `StateGraph` 中使用。

```python
reg = SkillRegistry(mode="lazy").scan()     # 只读 frontmatter
reg.load("search-strategy")                  # 按需加载正文
reg.instructions(["search-strategy"])        # 注入文本
```

- 支持 `lazy` / `eager` / `off` 三模式，内建 token 与耗时计量
- 激活方式：**独立的一次 LLM 路由调用**，再把命中技能正文拼进后续 prompt
- 入口：`agent_brief.py --skills=lazy|eager|off`
- 局限：不符合 Agent Skills 规范、无 `references`/`scripts` 支持、无工具绑定与热重载

### ② `langchain-agentskills`（第三方社区包，已评估、未采用）

PyPI 上的 [langchain-agentskills](https://pypi.org/project/langchain-agentskills/)（v0.4.0，MIT，作者 AnhQuanTrl，**非 LangChain 官方**）：

```python
from langchain_agentskills import SkillsToolkit, DirectorySkillLoader, SkillMiddleware

toolkit = SkillsToolkit(directories=["./skills"])          # 拿到 tools + instructions
agent = create_agent(model=..., tools=toolkit.get_tools(),
                     system_prompt=toolkit.get_instructions())
# 或中间件式（推荐）
agent = create_agent(model=..., middleware=[SkillMiddleware(loader=DirectorySkillLoader("./skills"))])
```

- 优点：**零新增依赖**（`langchain-core` / `pydantic` / `pyyaml` / `httpx` 均已满足），
  与现有 `SKILL.md` **字段完全兼容**；middleware 自动注册 `load_skill` /
  `read_skill_resource` / `run_skill_script` 三个工具
- 未采用原因：属社区包（维护风险）、技能激活依赖模型主动调用加载工具、
  无工具绑定与 pin/热重载能力

### ③ `deepagents`（LangChain 官方，**本项目采用**）

`deepagents 0.7.32`，官方原生 Skills 支持，遵循 Agent Skills 规范并实现**完整三级披露**：

```python
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

backend = FilesystemBackend(root_dir=BASE_DIR, virtual_mode=True)
agent = create_deep_agent(model=llm, tools=TOOLS, backend=backend, skills=["/skills/"])
```

- 激活方式：`SkillsMiddleware` 启动注入元信息（`before_agent`），模型在循环中用
  **`read_file`** 读取命中的 `SKILL.md`（`before_model`）
- 入口：`agent_deepagents.py`
- 额外能力（本项目未使用）：工具绑定技能（`metadata.include_tools`）、
  pinned skills、线程内热重载（`skills_metadata=None`）

### 三方式横比

| 维度 | ① 自建 loader | ② langchain-agentskills | ③ deepagents |
| --- | --- | --- | --- |
| 性质 | 自建 | 第三方社区（v0.4.0） | **LangChain 官方（0.7.32）** |
| 接入代码量 | ~200 行 | 2–3 行 | **2–3 行** |
| 依赖增量 | **0** | **0** | **+18 个包，5 个包版本变动** ⚠️ |
| 规范符合性 | 自定约定 | ✅ | ✅ |
| 披露层级 | 2 级 | 2 级 + resources/scripts | ✅ 3 级完整 |
| 激活机制 | 独立路由 LLM 调用 | 模型调用 `load_skill` | 模型 `read_file` |
| 工具绑定技能 | ❌ | ❌ | ✅ |
| Pin / 热重载 | ❌ | ❌ | ✅ |
| 计量能力 | ✅ 内建 | 需接 trace | 需接 LangSmith |

## 四、实测数据

### 4.1 三种实现跑同一任务

同一主题「大模型上下文工程」、同一模型 `deepseek-v4-pro`、同一批技能文件与工具：

| 实现 | 耗时 | 输入 token | 输出 token | 工具调用 | 技能加载 |
| --- | --- | --- | --- | --- | --- |
| ① 自建 `lazy` | 386.6 s | **4 086** | 10 532 | —（固定流水线） | 3/5，正文 1213 token |
| ① 自建 `eager` | 512.1 s | 5 126 | 18 483 | — | 5/5，正文 1800 token |
| ③ **deepagents 原生** | **168.1 s** | **111 539** | 13 580 | **19**（`read_file` 5） | 5/5，由模型自行决定 |

### 4.2 自建实现的 lazy vs eager 受控对照

两次运行路由选中了**完全相同的 3 个技能**，唯一变量是加载模式：

| 指标 | `lazy` | `eager` | 变化 |
| --- | --- | --- | --- |
| 常驻目录 | 311 token | 311 token | 持平 |
| 注入技能正文 | 1213 token（3/5） | 1800 token（5/5） | +587 |
| 输入 token | 4 086 | 5 126 | **+25.5%** |
| 总耗时 | 386.6 s | 512.1 s | **+32.5%** |

### 4.3 四条结论

1. **按需加载理论上显著更省**：受控对照下省 25.5% 输入 token、32.5% 耗时；
   且差距随技能数线性扩大（eager 随技能总数增长，lazy 只随命中数增长）。
2. **但节省会被模型行为抵消**：deepagents 自主模式下，模型对同一任务
   **读取了全部 5 个技能的正文**（`read_file` 调用 5 次）。
   渐进式披露能否省钱，取决于模型是否"克制地只读相关技能"——本例中节省兑现为 0。
3. **自主 agent 循环的上下文成本呈平方级增长**：deepagents 共 27 条消息、19 次工具调用，
   每轮都要重发全部历史，**输入 token 达 111 539，是固定流水线的 27 倍**；
   但它**更快**（168 s vs 387 s），因为单次请求更短、无超长核验提示词。
   → **"更快"与"更省"是两个维度，不能混为一谈。**
4. **技能确实影响了输出质量**：deepagents 生成的简报中可观察到技能痕迹——
   `source-triage` 的 A/B/C 来源分级、`fact-cross-verify` 的「多来源一致 / 待核实」标注、
   `citation-format` 的链接去重与截断、`brief-writing` 的固定结构与字数说明。

### 4.4 引入官方实现的依赖代价（实测）

`uv add deepagents` 带来的环境变动：

| 变化 | 包 |
| --- | --- |
| 升级 | `langchain-core 1.6.12 → 1.7.0`、`langchain 1.4.6 → 1.4.7`、`langsmith`、`langchain-protocol` |
| **降级** | `tenacity 9.2.1 → 9.1.4` |
| 新增 18 个 | 含 `anthropic`、`google-genai`、`langchain-anthropic`、`wget-match` 等本项目用不到的包 |

升级后回归验证：`langchain_mcp_adapters` 可正常导入、`agent_brief.py` 状态图仍可编译，
**千问 MCP 链路未受影响**；但 `tenacity` 被降级属于非预期副作用，需在依赖锁定中留档。

## 五、复现

```powershell
# ③ deepagents 原生（本项目采用的实现）
python agent_deepagents.py "大模型上下文工程"

# ① 自建 loader 三模式对照
python agent_brief.py "大模型上下文工程" --skills=lazy
python agent_brief.py "大模型上下文工程" --skills=eager
python agent_brief.py "大模型上下文工程" --skills=off
```

数据来源：`output/stats_deepagents_*.json` 与 `output/stats_*.json`。

## 六、环境注意（实测踩到的坑）

`tiktoken` 首次使用需联网下载约 1.7 MB 词表：

| 时机 | `estimator_init_ms` |
| --- | --- |
| 首次（需下载词表） | 实测 **27 949 ms** |
| 之后（词表已缓存） | 约 **165 ms** |

自建实现已把「编码器初始化」与「技能扫描」**分开计时**，避免把 28 秒误判为技能层开销；
离线环境可设 `SKILL_TOKEN_ESTIMATOR=heuristic` 走字符启发式，但两种口径不可混用
（同一份技能：目录 311→195 token，正文 1800→1006 token）。