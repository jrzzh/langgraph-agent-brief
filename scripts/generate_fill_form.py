# -*- coding: utf-8 -*-
"""生成 LangGraph 单框架实测填表数据（11 个维度）

表中不写入任何个人信息（姓名 / 联系方式等）。
"""
import os

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "LangGraph单框架实测"

# 标题
ws.merge_cells("A1:E1")
c = ws["A1"]
c.value = "LangGraph 单框架实测填表数据"
c.font = Font(bold=True, size=14, name="微软雅黑")
c.alignment = Alignment(horizontal="center", vertical="center")
ws.row_dimensions[1].height = 28

headers = ["序号", "测试维度", "具体考察点", "实测结论 / 证据", "来源类型"]
header_fill = PatternFill("solid", fgColor="DDEBF7")
thin = Side(style="thin", color="999999")
border = Border(left=thin, right=thin, top=thin, bottom=thin)

for j, h in enumerate(headers, 1):
    cell = ws.cell(row=2, column=j, value=h)
    cell.font = Font(bold=True, name="微软雅黑")
    cell.fill = header_fill
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell.border = border
ws.row_dimensions[2].height = 22

rows = [
    (1, "上手难度",
     "环境安装、依赖冲突、最小示例跑通耗时、版本约束",
     "uv 创建 venv + 一条命令安装完成（约 45s），Python 3.13 完全兼容。"
     "依赖问题两处：lxml 6.x 移除内置 cssselect（改原生 XPath）；引入 deepagents 连带 +18 包（含 tenacity 降级）。"
     "新增 streamlit 图形界面依赖，连带安装 15 个包，不影响命令行主流程。"
     "最小示例（安装+冒烟测试）约 10 分钟跑通。",
     "实测"),
    (2, "Skills 封装方式",
     "封装形式（装饰器/配置/可视化）、技能复用粒度、新增自定义技能工作量",
     "三种形式并存：① 函数装饰器（@tool）封装工具；② 状态图节点函数封装流程步骤；"
     "③ **声明式技能单元**（skills/<name>/SKILL.md，frontmatter 含 name/description/metadata，"
     "符合 Agent Skills 规范）—— 无可视化节点。"
     "**技能实现评估了三种、并保留两套可运行代码**：自建 skill_loader（248 行、零依赖、可计量）、"
     "第三方 langchain-agentskills 0.4.0（零新增依赖，Toolkit / Middleware 两式，已评估未采用）、"
     "官方 **deepagents 0.7.32（最终采用）**，三级渐进式披露，接入仅 2–3 行。"
     "两套实现的本质差异是**技能选择权的归属**：自建用一次被强约束的 LLM 路由"
     "（首次因解析缺陷未命中、已修；修复后两次运行均选中同一组 3/5："
     "fact-cross-verify、search-strategy、source-triage），"
     "deepagents 由模型在主循环中自主 read_file（四次运行均读满 5/5）。"
     "复用粒度：工具函数级 / 图节点级 / 技能文件级（技能文件级不改代码即可新增）。"
     "新增技能工作量：仅需新建目录 + 1 个 Markdown 文件，零代码改动。"
     "结论：**裸 LangGraph 无 Skills 抽象，但 LangChain 生态的 deepagents 提供官方原生实现**，"
     "属「框架本体空缺、生态补齐」，与 MCP 情况结构同构；"
     "代价是 +18 个依赖包、5 个包版本变动（含 tenacity 降级 9.2.1→9.1.4）。",
     "实测"),
    (3, "MCP 支持",
     "原生支持/第三方适配器、MCP Server 接入复杂度、工具自动发现能力、调用稳定性",
     "非原生，靠第三方适配器 langchain-mcp-adapters 0.3.2，接入生产级 MCP 服务（千问AI平台，One Key 双服务）。"
     "接入复杂度低：MultiServerMCPClient 配置 url + headers，一次 get_tools() 自动发现 2 个工具"
     "（EnhancedSearch→search_pro、WebFetch→web_fetch），加载约 1.8~3.2s。"
     "传输为 Streamable HTTP + Bearer 鉴权；工具返回 content blocks 需解析；"
     "已实现失败重试 1 次 + 本地降级。",
     "实测"),
    (4, "工具加载时机",
     "启动全量加载 / 运行时按需加载、大量工具下内存与启动耗时表现",
     "三层加载时机：① 工具 —— 代码内 @tool 定义，随图编译注册（启动期）；"
     "② MCP 工具 —— 首次调用时 get_tools() 按需加载并缓存（双检锁防并发重复建会话），实测 2 个工具 1.8~3.2s；"
     "③ 技能 —— 启动仅注入元信息（5 个技能共 311 token、扫描约 11ms），正文按需加载（5 个正文共 1800 token）。"
     "**两套实现各跑多次**（同任务、同模型 deepseek-flash）："
     "自建 lazy 50~94s / 输入 4.3~6.2k token / 路由选中 3 个技能；"
     "deepagents 65~70s / 输入 75~85k token / 读满 5 个技能；"
     "LLM 调用次数：自建**恒定 4 次**（压缩时 5），deepagents **6~7 次**（= 行动轮数 + 1 轮写答案）。"
     "关键结论：① 两者耗时相当，但**调用次数与输入 token 差异巨大**——"
     "输入 token 相差 13~16 倍，源于「每步重拼窄 prompt」与「每轮重发全部历史」两种上下文策略；"
     "② 耗时主导因素是**单次调用的输出长度**（推理模型的思维链），不是调用次数；"
     "③ 按需加载的理论收益在自主模式下兑现为 0（模型会读满全部技能），披露收益取决于模型是否克制。",
     "实测"),
    (5, "参数校验",
     "类型与格式校验、错误拦截时机（运行前 / 工具调用时）",
     "基于 Pydantic 模型自动校验（StructuredTool）。拦截时机在工具调用时（invoke 抛 ValidationError），"
     "非图运行前；类型/缺参错误能被 Pydantic 清晰报出。",
     "实测"),
    (6, "错误处理恢复",
     "工具调用失败、网络异常、参数错误、权限受限表现；重试、降级、报错可读性；任务中断恢复能力",
     "框架无内置自动重试，需手动实现。本项目的恢复链路：MCP 失败 → 重试 1 次 → 降级本地实现，"
     "并统计后端命中。实测有效：13:31 运行中千问 MCP 3 个并发查询全部瞬时失败，自动降级 Bing 后任务完整跑完；"
     "定稿运行命中 MCP 3/3、降级 0 次。目标站点反爬亦由 MCP 抓取解决（读取成功 0/3 → 3/3）。"
     "报错经 ExceptionGroup 逐层展开为可读根因；计算器对非法表达式返回友好错误而非崩溃。",
     "实测"),
    (7, "调试可观测性",
     "Tracing 调用链路、prompt 与工具出入参查看、排错难易度",
     "官方 Tracing 靠 LangSmith（需额外 API key，未接入）。本地用 logging 实现节点级日志"
     "（记录查询词、结果数、各节点耗时、token）。每个节点完成时**自报耗时**，"
     "运行结束打印耗时表（秒数 / 占比 / 条形），可直接看出瓶颈；"
     "统计同时落盘 `output/stats_*.json`，图形界面可回看日志与统计。"
     "排错难易度中等：图异常 traceback 清晰，节点日志足够定位。",
     "实测"),
    (8, "多工具协作",
     "多工具并发调用支持、多工具结果整合、上下文状态维护",
     "支持并发：本次用 ThreadPoolExecutor 并发搜索 3 查询/抓取 3 网页；LangGraph 本身支持并行节点与 Send API。"
     "结果整合需手动在节点内合并（JSON 聚合）。上下文状态靠 State TypedDict 自动传递 + reducer 累积故障。",
     "实测"),
    (9, "模型兼容性",
     "是否绑定特定厂商、切换国产模型修改量",
     "不绑定厂商（LangChain 抽象层）。实测接入 DeepSeek（ChatOpenAI + base_url）。"
     "切换通义千问/其他 OpenAI 兼容模型只需改 model/api_key/base_url，代码改动极小；"
     "实测同任务更换模型（chat→v4-pro）仅改 1 个环境变量、代码零改动，但耗时 11.82s→97.20s（8.2×）、"
     "输出 token 1454→11059（7.6×）；后按同一配置再切 v4-pro→flash，总耗时 146.65s→84.21s（-42.6%）、"
     "输出 token 11568→9166（-21%），其中 brief 节点降幅最大（-69%）。"
     "两者均返回 reasoning token（flash 105 / v4-pro 154），即 flash 仍是推理模型、思维链更短。"
     "故横向比对必须锁定同一模型。",
     "文档+实测"),
    (10, "社区生态",
     "文档完整度、示例数量、插件工具生态、issue 活跃度",
     "官方文档完善、示例丰富；插件工具依托 LangChain 生态（丰富）；GitHub 活跃度高。"
     "注意：langchain-community 已进入 sunset 阶段（运行时弃用警告），部分工具需迁移到独立集成包。",
     "文档"),
    (11, "任务实测指标",
     "实现任务代码量/操作步骤、执行耗时、token 消耗、简报输出质量、BUG 与失败点记录",
     "核心代码 agent_brief.py 821 行 + agent_deepagents.py 433 行 + skill_loader.py 248 行"
     " + streamlit_app.py 345 行；运行 1 条 uv 命令（或界面一键运行）。"
     "代表运行总耗时 68.12s（plan 1.50 / skill_select 5.87 / search 3.74 / fetch 1.75 / verify 25.33 / brief 29.92）；"
     "LLM 调用 4 次、token 17374；简报正文 431 字（500 字仅约束正文，来源不计入）。"
     "耗时集中在 verify + brief（约 82%），根因是推理模型的思维链：实测输出 token 中约 77% 为 reasoning，"
     "且 max_tokens / reasoning_effort 均无法压缩它。"
     "BUG/失败点 17 项，其中 16 项已解决、1 项如实记录为未修复缺陷，见《04-故障记录》。",
     "实测"),
]

for i, (idx, dim, point, conclusion, source) in enumerate(rows, start=3):
    ws.cell(row=i, column=1, value=idx).alignment = Alignment(horizontal="center", vertical="top")
    ws.cell(row=i, column=2, value=dim).alignment = Alignment(horizontal="center", vertical="top", wrap_text=True)
    ws.cell(row=i, column=3, value=point).alignment = Alignment(vertical="top", wrap_text=True)
    ws.cell(row=i, column=4, value=conclusion).alignment = Alignment(vertical="top", wrap_text=True)
    ws.cell(row=i, column=5, value=source).alignment = Alignment(horizontal="center", vertical="top")
    for j in range(1, 6):
        ws.cell(row=i, column=j).border = border
        ws.cell(row=i, column=j).font = Font(name="微软雅黑", size=10)

# 列宽
ws.column_dimensions["A"].width = 6
ws.column_dimensions["B"].width = 15
ws.column_dimensions["C"].width = 34
ws.column_dimensions["D"].width = 62
ws.column_dimensions["E"].width = 11

# 行高（自动，给结论列足够空间）
for i in range(3, 3 + len(rows)):
    ws.row_dimensions[i].height = 95

ws.freeze_panes = "A3"

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "06-单框架实测填表数据.xlsx")
wb.save(OUT_FILE)
print("已生成 06-单框架实测填表数据.xlsx，共", len(rows), "个维度")
