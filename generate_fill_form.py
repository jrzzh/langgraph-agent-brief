# -*- coding: utf-8 -*-
"""生成 LangGraph 单框架实测填表数据（11 个维度）

署名通过环境变量注入，避免公开代码中含个人信息：
    REPORT_AUTHOR=张三  →  标题追加「（测试人员：张三）」
不设置该变量时不写入署名。
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
_AUTHOR = os.environ.get("REPORT_AUTHOR", "").strip()
c.value = "LangGraph 单框架实测填表数据" + (
    f"（测试人员：{_AUTHOR}）" if _AUTHOR else "")
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
     "唯一依赖问题是 lxml 6.x 移除内置 cssselect，改用原生 XPath 解决，非 LangGraph 本身。"
     "接入千问 MCP 无需新增依赖（langchain-mcp-adapters 已在依赖内），仅需配置 1 个 API Key。"
     "最小示例（安装+冒烟测试）约 10 分钟跑通。",
     "实测"),
    (2, "Skills 封装方式",
     "封装形式（装饰器/配置/可视化）、技能复用粒度、新增自定义技能工作量",
     "函数装饰器（@tool）+ 状态图节点函数，无可视化节点。复用粒度为工具函数级 + 图节点级。"
     "新增自定义技能只需 @tool 装饰器 + 函数体，工作量低（本次 3 个工具共约 60 行）。",
     "文档+实测"),
    (3, "MCP 支持",
     "原生支持/第三方适配器、MCP Server 接入复杂度、工具自动发现能力、调用稳定性",
     "非原生，靠第三方适配器 langchain-mcp-adapters 0.3.2，实测接入生产级 MCP 服务（千问AI平台）。"
     "接入复杂度低：MultiServerMCPClient 配置 url + headers，一次 get_tools() 自动发现 2 个工具"
     "（EnhancedSearch→search_pro、WebFetch→web_fetch），加载 3.19s。"
     "传输为 Streamable HTTP + Bearer 鉴权；本地 FastMCP Server 作为示教与对照组。"
     "调用稳定性：stdio 在受限环境失败（命名管道被禁），远程 HTTP 稳定；工具返回 content blocks 需解析；"
     "已实现失败重试 + 本地降级。",
     "实测"),
    (4, "工具加载时机",
     "启动全量加载 / 运行时按需加载、大量工具下内存与启动耗时表现",
     "启动期：代码内 @tool 定义、编译图时注册；运行时按需：千问 MCP 工具在首次调用时 get_tools() 加载并缓存"
     "（双检锁防并发重复建会话）。实测 2 个 MCP 工具加载 3.19s，此后各查询共用缓存。大量工具场景未实测。",
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
     "（记录查询词、结果数、各节点耗时、token）。工具出入参可自行打印。"
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
     "输出 token 1454→11059（7.6×）；根因是 v4-pro 为推理模型（返回 reasoning token），"
     "故横向比对必须锁定同一模型。",
     "文档+实测"),
    (10, "社区生态",
     "文档完整度、示例数量、插件工具生态、issue 活跃度",
     "官方文档完善、示例丰富；插件工具依托 LangChain 生态（丰富）；GitHub 活跃度高。"
     "注意：langchain-community 已进入 sunset 阶段（运行时弃用警告），部分工具需迁移到独立集成包。",
     "文档"),
    (11, "任务实测指标",
     "实现任务代码量/操作步骤、执行耗时、token 消耗、简报输出质量、BUG 与失败点记录",
     "核心代码 agent_brief.py 559 行 + MCP 演示（server 40 行 + client 79 行）119 行；运行 1 条命令。"
     "定稿运行总耗时 158.61s（plan 7.56 / search 4.63 / fetch 1.38 / verify 118.76 / brief 26.27）；"
     "多次运行的 verify 波动 3.4~119s（deepseek-v4-pro 为推理模型，思维链长度不定）。"
     "token 共 11481（in 2680 / out 8801），LLM 调用 3 次。"
     "简报正文 338 字（500 字仅约束正文，来源 430 字不计入），主题相关性高、来源 3/3 可读、零故障记录。"
     "BUG/失败点 10 项，且全部已解决或具备降级路径，见《04-故障记录》。",
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
