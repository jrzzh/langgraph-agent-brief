# -*- coding: utf-8 -*-
"""
LangGraph 统一任务实测脚本
================================================================
框架    : LangGraph 1.2.16  +  LangChain 1.4.6
模型    : DeepSeek (deepseek-flash)
统一任务: 联网检索资料 → 提取关键信息 → 事实交叉核验 → 生成 500 字以内
          带来源链接的 Markdown 技术简报
统一工具: 联网搜索 + 网页读取（均走千问 MCP，失败自动降级本地实现） + 计算器
================================================================
用法    : python agent_brief.py [主题]
示例    : python agent_brief.py 大模型上下文工程
"""

import ast
import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Annotated, TypedDict

import operator
import requests
from dotenv import load_dotenv
from lxml import html

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

try:                                    # 技能层：声明式 SKILL.md + 渐进式披露
    from skill_loader import SkillRegistry

    SKILLS_AVAILABLE = True
except Exception:                       # 技能层缺失时不影响主流程
    SkillRegistry = None                # type: ignore
    SKILLS_AVAILABLE = False

load_dotenv()

# ---------------------------------------------------------------------------
# 运行结果与统计目录
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

#: 覆盖运行日志目录的环境变量。被外部 UI（如 agent-ui）嵌入时由宿主指定，
#: 避免每跑一轮就往本项目仓库里塞一个日志文件。
LOG_DIR_ENV = "LANGGRAPH_BRIEF_LOG_DIR"

LOG_DIR = os.path.expanduser(os.environ.get(LOG_DIR_ENV) or os.path.join(BASE_DIR, "logs"))
OUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)


def set_log_dir(path: str | os.PathLike) -> str:
    """切换运行日志目录（宿主集成时调用，import 之后再设也生效）。"""
    global LOG_DIR
    LOG_DIR = os.path.expanduser(str(path))
    os.makedirs(LOG_DIR, exist_ok=True)
    return LOG_DIR


def _rel(path: str) -> str:
    """转成相对项目根目录的路径，避免日志中写入本机绝对路径（便于提交/分享）。"""
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:          # 跨盘符等无法求相对路径的情况
        return os.path.basename(path)


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格（中文字符占 2 列），让日志表格对齐。"""
    shown = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
    return text + " " * max(0, width - shown)


def _log_timing_table(total: float) -> None:
    """把各步骤耗时打印成表格：秒数 / 占比 / 条形，一眼看出瓶颈在哪。"""
    timings = STATS.get("node_timings") or {}
    if not timings:
        return
    rows = sorted(timings.items(), key=lambda kv: kv[1], reverse=True)
    node_sum = round(sum(v for _, v in rows), 2)
    log.info("---------- 各步骤耗时 ----------")
    for name, sec in rows:
        pct = (sec / total * 100) if total else 0.0
        bar = "█" * min(30, max(1, int(pct / 3)))
        log.info("  %s %7.2fs  %5.1f%%  %s", _pad(name, 14), sec, pct, bar)
    log.info("  %s %7.2fs  %5.1f%%", _pad("节点合计", 14), node_sum,
             100.0 if not total else node_sum / total * 100)
    others = round(max(0.0, total - node_sum), 2)
    log.info("  %s %7.2fs  %5.1f%%", _pad("图外开销", 14), others,
             0.0 if not total else others / total * 100)
    log.info("  %s %7.2fs", _pad("总耗时", 14), total)

# ---------------------------------------------------------------------------
# 日志配置（stdout + 文件，UTF-8）
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(LOG_DIR, f"run_{RUN_ID}.log")


#: 本项目的日志器。**不要用 "langgraph" 这个名字**——那是第三方库的命名空间，
#: 把它的级别设成 DEBUG 会连带打开框架内部的调试日志，被嵌进别的 UI 时尤其明显。
log = logging.getLogger("langgraph_brief")
log.setLevel(logging.DEBUG)
#: 不往宿主 root logger 冒泡：作为库被嵌入（如 agent-ui）时，日志只进本项目文件，
#: 不污染宿主的控制台。
log.propagate = False
_handlers_ready = False


def setup_logging(force: bool = False, *, console: bool = True) -> logging.Logger:
    """挂载文件与控制台 handler。

    延迟到 main()/run_task() 中调用，避免"仅 import 模块"时产生 0 字节日志文件。
    ``force=True`` 会先摘掉旧 handler 再重挂——同一进程内多次运行时切到新日志文件
    （Streamlit UI 会在一次会话里连续跑多次）。
    ``console=False`` 只写文件，供被外部 UI 嵌入的场景使用。
    """
    global _handlers_ready
    if _handlers_ready and not force:
        return log
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    for handler in list(log.handlers):        # 先摘旧 handler，避免重复输出
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")     # 文件
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if console:
        ch = logging.StreamHandler(sys.stdout)               # 控制台（仅命令行/自带界面）
        ch.setFormatter(fmt)
        log.addHandler(ch)
    _handlers_ready = True
    return log


def setup_run_logging(console: bool = True) -> logging.Logger:
    """为一次新运行开一个日志文件。

    ``console=True``（本项目命令行/自带界面）：文件 + 控制台双写。
    ``console=False``（被外部 UI 嵌入）：只写文件，**不输出到宿主控制台**。
    """
    global RUN_ID, LOG_FILE
    RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOG_FILE = os.path.join(LOG_DIR, f"run_{RUN_ID}.log")
    return setup_logging(force=True, console=console)


def setup_embedded_logging() -> logging.Logger:
    """被外部 UI 嵌入时启用：新日志文件 + 不打扰宿主控制台。"""
    return setup_run_logging(console=False)

# ---------------------------------------------------------------------------
# 全局统计（token 消耗、各节点耗时、故障）
# ---------------------------------------------------------------------------
STATS = {
    "token_input": 0,
    "token_output": 0,
    "llm_calls": 0,
    "node_timings": {},
    "tool_calls": {},
    "search_backend": {"qwen_mcp": 0, "bing_fallback": 0},
    "fetch_backend": {"qwen_mcp": 0, "local_fallback": 0},
    "errors": [],
    "skills": {},
}


def fresh_stats() -> dict:
    """每次运行重置统计——同一进程内可连续跑多次（Streamlit UI 需要）。"""
    return {
        "token_input": 0,
        "token_output": 0,
        "llm_calls": 0,
        "node_timings": {},
        "tool_calls": {},
        "search_backend": {"qwen_mcp": 0, "bing_fallback": 0},
        "fetch_backend": {"qwen_mcp": 0, "local_fallback": 0},
        "errors": [],
        "skills": {},
    }


def record_usage(resp) -> None:
    """从 LLM 响应中累计 token 用量。"""
    u = getattr(resp, "usage_metadata", None) or {}
    STATS["llm_calls"] += 1
    STATS["token_input"] += int(u.get("input_tokens", 0))
    STATS["token_output"] += int(u.get("output_tokens", 0))


# ---------------------------------------------------------------------------
# 统一工具集（LangChain @tool 装饰器，即 LangGraph 的“工具即节点函数”）
# ---------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# 联网工具后端：千问 MCP（主） → 本地实现（降级兜底）
#   EnhancedSearch/mcp 提供 search_pro（联网搜索增强版）
#   WebFetch/mcp       提供 web_fetch（标准化 Markdown 正文抽取）
QWEN_MCP_BASE = "https://maas.qianwenaiapi.com/api/v1/mcps"
_qwen_tools: dict = {}
_qwen_lock = threading.Lock()


async def _load_qwen_tools() -> dict:
    """经 langchain-mcp-adapters 接入千问 MCP 服务（Streamable HTTP）。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY")
    auth = {"Authorization": f"Bearer {key}"}
    client = MultiServerMCPClient({
        "qwen-search": {"transport": "streamable_http",
                        "url": f"{QWEN_MCP_BASE}/EnhancedSearch/mcp",
                        "headers": auth},
        "qwen-fetch": {"transport": "streamable_http",
                       "url": f"{QWEN_MCP_BASE}/WebFetch/mcp",
                       "headers": auth},
    })
    return {t.name: t for t in await client.get_tools()}


def _ensure_qwen_tools() -> dict:
    """按需加载并缓存千问 MCP 工具（双检锁，避免并发重复建会话）。"""
    if not _qwen_tools:
        with _qwen_lock:
            if not _qwen_tools:
                t0 = time.time()
                _qwen_tools.update(asyncio.run(_load_qwen_tools()))
                log.info("千问 MCP 工具加载完成: %s (%.2fs)",
                         list(_qwen_tools), time.time() - t0)
    return _qwen_tools


def _blocks_to_text(raw) -> str:
    """把 MCP 返回的 content blocks 拼成纯文本。"""
    blocks = raw if isinstance(raw, list) else [raw]
    text = ""
    for b in blocks:
        text += b.get("text", "") if isinstance(b, dict) else (getattr(b, "text", "") or "")
    return text


def _qwen_search(query: str) -> str:
    """主后端：千问 EnhancedSearch 的 search_pro 工具，输出统一的 JSON 结构。"""
    tools = _ensure_qwen_tools()
    tool_obj = tools.get("search_pro")
    if tool_obj is None:
        raise RuntimeError(f"千问 MCP 未提供 search_pro（现有: {list(tools)}）")
    text = _blocks_to_text(asyncio.run(tool_obj.ainvoke({"query": query})))
    pages = json.loads(text).get("pages", [])
    results = [{"title": p.get("title", ""), "url": p.get("url", ""),
                "snippet": p.get("snippet", "")} for p in pages]
    return json.dumps(results[:5], ensure_ascii=False)


def _qwen_fetch(url: str) -> str:
    """主后端：千问 WebFetch 的 web_fetch 工具，返回标准化 Markdown 正文。"""
    tools = _ensure_qwen_tools()
    tool_obj = tools.get("web_fetch")
    if tool_obj is None:
        raise RuntimeError(f"千问 MCP 未提供 web_fetch（现有: {list(tools)}）")
    data = json.loads(_blocks_to_text(asyncio.run(tool_obj.ainvoke({"url": url}))))
    return data.get("content", "")


#: XML 声明：lxml 不接受"str 输入 + 声明里带 encoding"，必须先剥掉
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>\s*", re.I)


def _parse_html(text: str):
    """把已解码的 HTML 字符串交给 lxml。

    lxml 对 ``str`` 输入有个硬限制：若文档开头带 ``<?xml ... encoding=...?>``，
    会直接抛 ``ValueError: Unicode strings with encoding declaration are not supported``。
    这类页面（XHTML、部分站点的模板）并不少见，所以统一先剥掉声明再解析。
    """
    return html.fromstring(_XML_DECL.sub("", text, count=1))


def _bing_search(query: str) -> str:
    """降级后端：Bing HTML 抓取。"""
    r = requests.get("https://cn.bing.com/search",
                     params={"q": query, "count": 10},
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    tree = _parse_html(r.text)
    results = []
    for li in tree.xpath('//li[contains(@class, "b_algo")]'):
        a = li.xpath('.//h2/a')
        if not a:
            continue
        p = li.xpath('.//p')
        results.append({"title": a[0].text_content().strip(),
                        "url": a[0].get("href", ""),
                        "snippet": p[0].text_content().strip() if p else ""})
    return json.dumps(results[:5], ensure_ascii=False)


def _root_cause(exc: BaseException) -> str:
    """把 ExceptionGroup 逐层展开为最内层根因；多项并列时只列一条并注明数量。"""
    leaves: list[str] = []

    def walk(e: BaseException) -> None:
        subs = getattr(e, "exceptions", None)
        if subs:
            for s in subs:
                walk(s)
        else:
            leaves.append(f"{type(e).__name__}: {e}")

    walk(exc)
    uniq = list(dict.fromkeys(leaves))
    if not uniq:
        return type(exc).__name__
    if len(uniq) == 1:
        return uniq[0]
    return f"{uniq[0]}（另有 {len(uniq) - 1} 项同类失败）"


@tool
def web_search(query: str) -> str:
    """联网搜索指定关键词，返回多条结果的标题、链接与摘要（JSON 数组）。
    主后端：千问 EnhancedSearch MCP（联网搜索增强版）；失败重试 1 次后降级 Bing。"""
    last = None
    for attempt in (1, 2):
        try:
            out = _qwen_search(query)
            STATS["search_backend"]["qwen_mcp"] += 1
            return out
        except Exception as e:
            last = e
            if attempt == 1:
                log.warning("千问 EnhancedSearch 第 1 次失败(%s)，重试…", _root_cause(e))
                time.sleep(1.0)
    STATS["search_backend"]["bing_fallback"] += 1
    msg = f"千问 EnhancedSearch 失败({_root_cause(last)})，已降级 Bing"
    log.warning(msg)
    STATS["errors"].append(msg)
    # 工具**绝不抛异常**：模型直接调用的场景（如 deepagents）里，一次 ETL/网络失败
    # 会直接把整轮打断；返回错误文本让模型自行换策略才是正确姿势。
    try:
        return _bing_search(query)
    except Exception as e:                          # noqa: BLE001
        reason = _root_cause(e)
        STATS["errors"].append(f"Bing 降级也失败: {reason}")
        log.warning("Bing 降级也失败: %s", reason)
        return f"[检索失败: {reason}]"


def _http_fetch(url: str) -> str:
    """降级后端：requests + lxml 提取正文纯文本。"""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15,
                     allow_redirects=True)
    r.raise_for_status()
    # 处理常见编码
    if r.encoding and r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding
    tree = _parse_html(r.text)
    for bad in tree.xpath('//script | //style | //nav | //footer | //header | //aside'):
        bad.drop_tree()
    text = " ".join(p.text_content().strip() for p in tree.xpath('//p'))
    return re.sub(r"\s+", " ", text).strip()[:3000]


@tool
def web_fetch(url: str) -> str:
    """读取指定网页正文（截取前 3000 字符）。
    主后端：千问 MCP web_fetch（标准化 Markdown）；失败重试 1 次后降级本地抓取。"""
    last = None
    for attempt in (1, 2):
        try:
            out = _qwen_fetch(url)
            STATS["fetch_backend"]["qwen_mcp"] += 1
            return out[:3000]
        except Exception as e:
            last = e
            if attempt == 1:
                log.warning("千问 web_fetch 第 1 次失败(%s)，重试…", _root_cause(e))
                time.sleep(1.0)
    STATS["fetch_backend"]["local_fallback"] += 1
    msg = f"千问 web_fetch 失败({_root_cause(last)})，已降级本地抓取: {url}"
    log.warning(msg)
    STATS["errors"].append(msg)
    # 同上：本地抓取失败（404/429/超时/解析失败）也不能抛出去
    try:
        return _http_fetch(url)
    except Exception as e:                          # noqa: BLE001
        reason = _root_cause(e)
        STATS["errors"].append(f"本地抓取也失败({url}): {reason}")
        log.warning("本地抓取也失败 %s: %s", url, reason)
        return f"[读取失败: {reason}]"


@tool
def calculator(expression: str) -> str:
    """计算数学表达式，仅支持 + - * / ** % 与括号，返回数值结果。"""
    try:
        tree = ast.parse(expression, mode="eval")
        allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                   ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
                   ast.USub, ast.UAdd)
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                return f"错误：不允许的操作 {type(node).__name__}"
        result = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})
        return str(result)
    except Exception as e:
        return f"计算错误：{e}"


BASE_TOOLS = [web_search, web_fetch, calculator]

#: 可选的 workspace 文件读写工具（默认不启用，需显式开启）
try:
    from file_tools import (
        FILE_TOOLS,
        collect_sources,
        list_workspace_files,
        read_file_impl,
        set_workspace_root,
        use_workspace,
        validate_workspace,
        write_file_impl,
        workspace_root,
    )

    FILES_AVAILABLE = True
except Exception:                       # 文件工具缺失时不影响主流程
    FILE_TOOLS = []                     # type: ignore
    FILES_AVAILABLE = False

#: 可选的只读 shell 命令工具（默认不启用，白名单极窄）
try:
    from shell_tools import (
        SHELL_TOOLS,
        allowed_commands_text,
        shell_impl,
    )

    SHELL_AVAILABLE = True
except Exception:                       # shell 工具缺失时不影响主流程
    SHELL_TOOLS = []                    # type: ignore
    SHELL_AVAILABLE = False

#: 兼容既有引用（deepagents 等模块 import 的是这个名字）
TOOLS = list(BASE_TOOLS)


def get_tools(enable_files: bool = False, enable_shell: bool = False) -> list:
    """返回本次运行可用的工具集。

    基础三件套始终可用；``enable_files`` 追加 read_file / write_file；
    ``enable_shell`` 追加只读 shell 命令工具。
    """
    tools = list(BASE_TOOLS)
    if enable_files and FILES_AVAILABLE:
        tools.extend(FILE_TOOLS)
    if enable_shell and SHELL_AVAILABLE:
        tools.extend(SHELL_TOOLS)
    return tools


# ---------------------------------------------------------------------------
# 图状态定义
# ---------------------------------------------------------------------------
class BriefState(TypedDict):
    topic: str
    queries: list[str]
    search_results: str          # JSON 字符串
    fetched: str                 # 拼接的网页正文
    source_material: str         # workspace/sources 提供的补充材料（启用文件工具时）
    facts: str                   # 交叉核验结论
    brief: str                   # 最终简报
    skills_used: list[str]       # 技能路由选中的技能名
    skill_instructions: str      # 注入后续节点的技能正文（渐进式披露的产物）
    errors: Annotated[list[str], operator.add]  # 故障累积


# ---------------------------------------------------------------------------
# LLM 实例：采样温度与最大输出 token
# ---------------------------------------------------------------------------
DEFAULT_TEMPERATURE = 0.3
TEMPERATURE_MIN, TEMPERATURE_MAX = 0.0, 2.0

#: 默认不限制输出长度（沿用服务端默认）。本任务单次运行输出可达 1~3 万 token，
#: 设一个偏小的默认值会直接把简报截断，因此这里保持 None。
DEFAULT_MAX_TOKENS: int | None = None
MAX_TOKENS_MIN, MAX_TOKENS_MAX = 1, 200_000

#: 环境里没有密钥时的占位值：只保证 import 与构造不炸，请求时会被服务端拒绝
_PLACEHOLDER_KEY = "not-configured"

#: 当前生效的采样参数（两个设置项各自独立，共用同一次重建）
_temperature: float = DEFAULT_TEMPERATURE
_max_tokens: int | None = DEFAULT_MAX_TOKENS


def _build_llm(temperature: float, max_tokens: int | None = None, *,
               model: str | None = None, api_key: str | None = None,
               base_url: str | None = None) -> ChatOpenAI:
    """按给定参数构造模型实例。

    连接信息优先用显式传入的值，其次读环境变量。**缺 key 时用占位值**而不是抛异常：
    这样 ``import agent_brief`` 在没有配置密钥的环境里也能成功（供外部 UI 做懒加载），
    真正的失败推迟到第一次请求——比在 import 阶段 KeyError 友好得多。
    """
    kwargs = dict(
        model=model or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"),
        api_key=api_key or os.environ.get("DEEPSEEK_API_KEY") or _PLACEHOLDER_KEY,
        base_url=base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        temperature=temperature,
        timeout=60,
    )
    if max_tokens is not None:          # None = 不传，沿用服务端默认
        kwargs["max_tokens"] = int(max_tokens)
    return ChatOpenAI(**kwargs)


def build_llm(model: str | None = None, api_key: str | None = None,
              base_url: str | None = None, temperature: float | None = None,
              max_tokens: int | None = None) -> ChatOpenAI:
    """外部集成入口：返回一个独立的模型实例，**不改动全局 llm**。

    供通用 UI（如 agent-ui）按每轮的 ``options`` 现构造模型——它的密钥来自界面上的
    模型档案，不在环境变量里。
    """
    return _build_llm(
        DEFAULT_TEMPERATURE if temperature is None else float(temperature),
        None if max_tokens is None else int(max_tokens),
        model=model, api_key=api_key, base_url=base_url,
    )


def _rebuild() -> ChatOpenAI:
    """按当前生效的采样参数重建实例。"""
    global llm
    llm = _build_llm(_temperature, _max_tokens)
    return llm


#: 全局实例；节点通过模块全局名读取，故重建后立即生效
llm = _build_llm(_temperature, _max_tokens)


def get_llm() -> ChatOpenAI:
    """返回当前生效的模型实例（参数可能已被 set_* 改过）。"""
    return llm


def set_temperature(value: float | str) -> float:
    """调整采样温度（0.0 ~ 2.0），重建模型实例并返回实际生效值。

    温度越高输出越发散。本任务里 ``verify`` / ``brief`` 对稳定性敏感：
    低于 0.2 容易句式雷同，高于 0.8 容易在数字与来源上跑偏，建议 0.2~0.7。
    """
    global _temperature
    try:
        t = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"temperature 必须是数字，收到 {value!r}") from exc
    if not (TEMPERATURE_MIN <= t <= TEMPERATURE_MAX):
        raise ValueError(
            f"temperature 必须在 {TEMPERATURE_MIN}~{TEMPERATURE_MAX} 之间，收到 {t}"
        )
    _temperature = round(t, 3)
    _rebuild()
    return _temperature


def set_max_tokens(value: int | str | None) -> int | None:
    """调整单次输出上限（1 ~ 200000），``None`` 表示不限；返回实际生效值。

    注意本任务单次运行输出常在 6 千~3 万 token 之间，**设得过小会截断简报**，
    因此默认不限制。
    """
    global _max_tokens
    if value is None or (isinstance(value, str) and not value.strip()):
        _max_tokens = None
        _rebuild()
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"max_tokens 必须是整数，收到 {value!r}") from exc
    if not (MAX_TOKENS_MIN <= n <= MAX_TOKENS_MAX):
        raise ValueError(
            f"max_tokens 必须在 {MAX_TOKENS_MIN}~{MAX_TOKENS_MAX} 之间，收到 {n}"
        )
    _max_tokens = n
    _rebuild()
    return _max_tokens


def current_temperature() -> float:
    """当前生效的采样温度。"""
    return _temperature


def current_max_tokens() -> int | None:
    """当前生效的输出上限；``None`` 表示不限。"""
    return _max_tokens

# ---------------------------------------------------------------------------
# 图节点
# ---------------------------------------------------------------------------
def plan_node(state: BriefState) -> dict:
    """节点1：把主题规划为 3 个搜索关键词。"""
    t0 = time.time()
    topic = state["topic"]
    prompt = (
        f"主题：“{topic}”。请给出 3 个用于检索该主题资料的搜索关键词，"
        f"每行一个，只输出关键词本身，不要编号和解释。"
    )
    resp = llm.invoke(prompt)
    record_usage(resp)
    queries = [q.strip() for q in resp.content.splitlines() if q.strip()][:3]
    if not queries:
        queries = [topic]
    STATS["node_timings"]["plan"] = round(time.time() - t0, 2)
    log.info("plan 完成 | 生成查询 %s | 耗时 %.2fs",
             queries, STATS["node_timings"]["plan"])
    return {"queries": queries}


# 技能注册表：main() 中按 --skills 初始化；为 None 表示技能层未启用
REGISTRY = None


def skill_select_node(state: BriefState) -> dict:
    """节点2：技能路由 —— 仅凭「技能目录」（元信息）挑技能，再按需加载其正文。

    这是渐进式披露的核心：目录常驻上下文，技能正文只在被选中时才读入。
    eager 模式下正文已在启动时全部装好，此处仍执行同样的路由（保证对照公平），
    但注入的是**全量**正文 —— 用于实测「启动全量 vs 运行时按需」的上下文成本差异。
    """
    t0 = time.time()
    if REGISTRY is None or REGISTRY.mode == "off" or not REGISTRY.skills:
        STATS["node_timings"]["skill_select"] = round(time.time() - t0, 2)
        log.info("skill_select 跳过（技能层未启用或无技能） | 耗时 %.2fs",
                 STATS["node_timings"]["skill_select"])
        return {"skills_used": [], "skill_instructions": ""}

    chosen: list[str] = []
    try:
        resp = llm.invoke(
            "你是技能路由器。下面是可用技能目录（仅元信息，正文未加载）。\n"
            "请根据主题选出最相关的技能，最多 3 个。\n"
            "只输出技能名，每行一个，不要编号与解释；没有合适的就输出 NONE。\n\n"
            f"{REGISTRY.catalog_text()}\n\n主题：“{state['topic']}”"
        )
        record_usage(resp)
        raw = (resp.content or "").strip()
        log.info("技能路由原始输出: %s", raw.replace("\n", " | ")[:200])
        # 容错解析：大小写不敏感，允许被引号/括号/编号/中文注释包裹
        if raw and raw.upper() != "NONE":
            low = raw.lower()
            for name in REGISTRY.skills:
                if re.search(rf"(?<![a-z0-9\-]){re.escape(name)}(?![a-z0-9\-])", low) \
                        and name not in chosen:
                    chosen.append(name)
            chosen = chosen[:3]
    except Exception as e:
        STATS["errors"].append(f"技能路由失败: {type(e).__name__}: {e}")
        log.warning("技能路由失败，降级为不注入技能: %s", e)

    if REGISTRY.mode == "lazy":
        for n in chosen:
            REGISTRY.load(n)                    # ← 按需加载正文（渐进式披露第二级）
        instr = REGISTRY.instructions(chosen)
    else:                                        # eager：全量正文已就绪
        instr = REGISTRY.instructions()

    STATS["node_timings"]["skill_select"] = round(time.time() - t0, 2)
    log.info("skill_select 完成 | 选中 %s | 注入正文 %d 字符（mode=%s） | 耗时 %.2fs",
             chosen, len(instr), REGISTRY.mode, STATS["node_timings"]["skill_select"])
    return {"skills_used": chosen, "skill_instructions": instr}


def search_node(state: BriefState) -> dict:
    """节点2：对多个查询并发执行联网搜索（多工具/多查询并发）。"""
    t0 = time.time()
    queries = state.get("queries") or [state["topic"]]
    merged = {}
    errors = []

    def _run(q):
        for attempt in range(1, 4):  # 重试 3 次（错误处理恢复）
            try:
                raw = web_search.invoke({"query": q})
                return q, raw
            except Exception as e:
                log.warning("搜索重试 %d/%d query=%s err=%s", attempt, 3, q, e)
                time.sleep(1.5 * attempt)
        return q, None

    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_run, q): q for q in queries}
        for f in as_completed(futs):
            q, raw = f.result()
            merged[q] = raw
            if raw is None:
                errors.append(f"搜索失败(重试3次后): {q}")

    all_items = []
    for q, raw in merged.items():
        if not raw:
            continue
        try:
            items = json.loads(raw)
            all_items.extend(items)
        except Exception:
            pass
    STATS["node_timings"]["search"] = round(time.time() - t0, 2)
    STATS["tool_calls"]["web_search"] = len(queries)
    log.info("search 完成 | %d 个查询 → %d 条结果 | 耗时 %.2fs",
             len(queries), len(all_items), STATS["node_timings"]["search"])
    return {"search_results": json.dumps(all_items, ensure_ascii=False),
            "errors": errors}


def fetch_node(state: BriefState) -> dict:
    """节点3：并发读取搜索结果中前 3 条正文（网页读取工具）。"""
    t0 = time.time()
    errors = []
    try:
        items = json.loads(state.get("search_results") or "[]")
    except Exception:
        items = []
    urls = [it.get("url") for it in items if it.get("url")][:3]

    def _fetch(u):
        try:
            return web_fetch.invoke({"url": u})
        except Exception as e:
            return f"[读取失败: {type(e).__name__}]"

    chunks = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_fetch, u): u for u in urls}
        for f in as_completed(futs):
            u = futs[f]
            txt = f.result()
            if txt.startswith("[读取失败"):
                errors.append(f"网页读取失败: {u}")
            chunks.append(f"### 来源 {u}\n{txt[:800]}")
    STATS["node_timings"]["fetch"] = round(time.time() - t0, 2)
    STATS["tool_calls"]["web_fetch"] = len(urls)
    log.info("fetch 完成 | 读取 %d 个网页 | 耗时 %.2fs",
             len(urls), STATS["node_timings"]["fetch"])
    return {"fetched": "\n\n".join(chunks), "errors": errors}


def verify_node(state: BriefState) -> dict:
    """节点4：把材料整理成「讲清这个概念所需的知识点」，并顺手核对证据。

    注意：本节点的产物是**给写作环节用的素材**，不是给读者看的核验报告。
    证据强度只以标签形式附着在知识点上，不单独成段。
    """
    t0 = time.time()
    topic = state["topic"]
    results = state.get("search_results") or "[]"
    fetched = state.get("fetched") or ""
    prompt = (
        f"主题：“{topic}”。下面是检索到的材料。请把它整理成**讲清这个概念所需的知识点**，"
        f"按下面五类归拢；整理时顺手核对证据，但只打标签，不要写核对过程。\n\n"
        f"1. 定义：它是什么？一句话说清，并点明所属领域、解决什么问题。\n"
        f"2. 构成 / 机制：由哪几部分组成，各部分起什么作用、如何配合。\n"
        f"3. 运作流程：从输入到产出，典型步骤是什么。\n"
        f"4. 实践要点：落地时的关键做法、参数、取舍或常见工具。\n"
        f"5. 边界与误区：不适用于什么、容易被误解在哪里。\n\n"
        f"要求：\n"
        f"- 每条用自己的话概括，不要照抄摘要措辞；\n"
        f"- 每条末尾用方括号标证据强度：[多来源一致] / [单一来源] / [存疑]；\n"
        f"- 某一类材料里没有，就写“材料未覆盖”，不要编造；\n"
        f"- **不要**输出“已核验”“待核实”“证据强度如下”这类流程性叙述；\n"
        f"- 输出**总长控制在 500 字以内**，每类 1~2 条、每条一句话，不要复述材料原文。\n\n"
        f"【检索摘要】\n{results[:1500]}\n\n【网页正文摘录】\n{fetched[:1500]}"
        + (f"\n\n【workspace 补充材料】\n{(state.get('source_material') or '')[:3000]}"
           if (state.get("source_material") or "").strip() else "")
    )
    skill_txt = state.get("skill_instructions") or ""      # 渐进式披露：注入已加载技能
    if skill_txt:
        prompt = f"【已加载技能】\n{skill_txt}\n\n{prompt}"
    resp = llm.invoke(prompt)
    record_usage(resp)
    STATS["node_timings"]["verify"] = round(time.time() - t0, 2)
    log.info("verify 完成 | 知识点 %d 字符 | 耗时 %.2fs",
             len(resp.content), STATS["node_timings"]["verify"])
    return {"facts": resp.content}


# 来源小节识别：支持 "## 来源" / "**来源**" / "来源：" / "参考来源" / "来源（Sources）" 等写法
SRC_HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:\*\*)?\s*(?:参考)?(?:来源|来源链接|参考资料|Sources|References)"
    r"(?:\s*[（(][^)）]*[)）])?(?:\*\*)?\s*[:：]?\s*$", re.M | re.I)


def split_body_sources(brief: str) -> tuple[str, str]:
    """把简报拆成 (正文, 来源小节)。“来源”小节不计入 500 字限制。"""
    m = SRC_HEADING_RE.search(brief)
    if not m:
        return brief, ""
    return brief[:m.start()], brief[m.start():]


def count_body_chars(text: str) -> int:
    """统计正文字数：排除“来源”小节，去 Markdown 语法（链接保留文字）、去空白后计字符。"""
    body, _ = split_body_sources(text)
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    body = re.sub(r"[#*>`\-|]", "", body)
    body = re.sub(r"\s+", "", body)
    return len(body)


def brief_node(state: BriefState) -> dict:
    """节点5：生成技术简报，**正文** ≤500 字（“来源”小节不计入），末尾附来源链接。"""
    t0 = time.time()
    topic = state["topic"]
    facts = state.get("facts") or ""
    results = state.get("search_results") or "[]"
    try:
        items = json.loads(results)
        srcs = "\n".join(f"- [{it.get('title')}]({it.get('url')})"
                         for it in items[:5] if it.get("url"))
    except Exception:
        srcs = ""
    prompt = (
        f"请写一份**让第一次接触这个主题的读者也能看懂**的中文技术简报，主题：“{topic}”。\n\n"
        f"读者是想快速搞懂这个概念的人，**不是来审你的证据链的**。所以：\n"
        f"- 重点讲清「是什么、为什么重要、怎么运作、怎么用」；\n"
        f"- **不要**出现“核验结论”“证据强度”“单一来源”“待核实”这类成段论述；\n"
        f"- 只有关键数字或结论确实拿不准时，才用一句括注（如“（该数值待核实）”），全文最多 1~2 处。\n\n"
        f"结构（Markdown）：\n"
        f"1. 一级标题＝主题名\n"
        f"2. **一句话定义**：是什么 + 所属领域 / 解决什么问题（≤60 字）\n"
        f"3. **为什么重要**：2 句，讲它替代了什么、解决什么痛点\n"
        f"4. **怎么运作**：2~3 点，每点「小标题 + 一句解释」，**要写出具体机制或步骤**"
        f"（例如「写入—存储—召回—注入」这样的链路，或「短期缓冲 / 长期存储」这样的层次），不要只说作用\n"
        f"5. **实践要点**：2~3 条**具体可操作**的做法或选型判断，避免“需结合场景”这类空话\n"
        f"6. 末尾「来源」小节\n\n"
        f"硬性要求：\n"
        f"1) 正文（不含一级标题与“来源”小节）**目标 380~420 字，硬上限 460 字**；"
        f"宁可少写一条实践要点，也不要超限（超限会触发重写，反而更慢更乱）；\n"
        f"2) 语言平实少术语，必须出现的术语紧跟一句解释；\n"
        f"3) “来源”小节原样列出给定链接，不要增删；\n"
        f"4) 材料确实说不清的部分直接略过，不要用“资料不足”占篇幅。\n\n"
        f"【知识点材料】\n{facts}\n\n【可用来源链接】\n{srcs}"
    )
    skill_txt = state.get("skill_instructions") or ""      # 渐进式披露：注入已加载技能
    if skill_txt:
        prompt = f"【已加载技能】\n{skill_txt}\n\n{prompt}"
    resp = llm.invoke(prompt)
    record_usage(resp)
    brief = resp.content

    # 字数自检：仅统计正文（“来源”小节不计入 500 字限制）
    n = count_body_chars(brief)
    if n > 500:
        STATS["errors"].append(f"简报正文首次生成超字数({n}字)，已触发压缩")
    attempt = 0
    while count_body_chars(brief) > 500 and attempt < 3:
        attempt += 1
        cur = count_body_chars(brief)
        log.warning("简报正文 %d 字超限（来源不计入），第 %d 次压缩", cur, attempt)
        p2 = (f"以下简报的**正文**（不含“来源”小节）为 {cur} 字，需压缩到 400 字以内。\n"
              f"要求：\n"
              f"1) **完整保留原有结构**——一级标题，以及「一句话定义 / 为什么重要 / 怎么运作 / 实践要点」"
              f"这些小标题与分点层级都不能删，只压缩每个部分内部的文字；\n"
              f"2) 删修饰语、重复表述与次要例证，但**不删关键机制与具体做法**；\n"
              f"3) “来源”小节及其中的链接必须原样保留。\n\n{brief}")
        resp2 = llm.invoke(p2)
        record_usage(resp2)
        brief = resp2.content

    _, src_txt = split_body_sources(brief)
    src_chars = len(re.sub(r"\s+", "", src_txt))
    log.info("简报定稿：正文 %d 字（≤500），来源 %d 字不计入，压缩 %d 次",
             count_body_chars(brief), src_chars, attempt)

    STATS["node_timings"]["brief"] = round(time.time() - t0, 2)
    log.info("brief 完成 | 全文 %d 字符 | 正文 %d 字 | 来源 %d 字 | 耗时 %.2fs",
             len(brief), count_body_chars(brief), src_chars,
             STATS["node_timings"]["brief"])
    return {"brief": brief}


# ---------------------------------------------------------------------------
# 组装状态图
# ---------------------------------------------------------------------------
def build_graph():
    g = StateGraph(BriefState)
    g.add_node("plan", plan_node)
    g.add_node("skill_select", skill_select_node)
    g.add_node("search", search_node)
    g.add_node("fetch", fetch_node)
    g.add_node("verify", verify_node)
    g.add_node("brief", brief_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "skill_select")
    g.add_edge("skill_select", "search")
    g.add_edge("search", "fetch")
    g.add_edge("fetch", "verify")
    g.add_edge("verify", "brief")
    g.add_edge("brief", END)
    return g.compile()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _parse_args() -> tuple[str, str, bool, float | None, int | None, str | None, bool]:
    """解析命令行。

    位置参数为主题；``--skills=lazy|eager|off`` 选择技能加载模式；
    ``--files`` 开启 workspace 文件读写；``--temperature=0.3`` 调整采样温度；
    ``--max-tokens=4096`` 限制单次输出（默认不限）；``--workspace=路径`` 指定文件根目录；
    ``--shell`` 开启只读 shell 命令工具。
    """
    topic, mode, files = "大模型上下文工程", "lazy", False
    shell = False
    temperature: float | None = None
    max_tokens: int | None = None
    workspace: str | None = None
    for a in sys.argv[1:]:
        if a.startswith("--skills="):
            mode = a.split("=", 1)[1].strip().lower()
        elif a.startswith("--temperature="):
            try:
                temperature = float(a.split("=", 1)[1].strip())
            except ValueError:
                raise SystemExit(f"--temperature 需要数字，收到 {a!r}")
        elif a.startswith("--max-tokens="):
            raw = a.split("=", 1)[1].strip()
            try:
                max_tokens = int(raw)
            except ValueError:
                raise SystemExit(f"--max-tokens 需要整数，收到 {a!r}")
        elif a.startswith("--workspace="):
            workspace = a.split("=", 1)[1].strip()
        elif a in ("--files", "--file-tools"):
            files = True
        elif a in ("--shell", "--shell-tools"):
            shell = True
        elif not a.startswith("-"):
            topic = a
    if mode not in ("lazy", "eager", "off"):
        mode = "lazy"
return topic, mode, files, temperature, max_tokens, workspace, shell


def start_run() -> str:
    """为一次运行分配新的 RUN_ID 并重挂日志（同进程可连续跑多次，UI 需要）。"""
    global RUN_ID, LOG_FILE
    RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOG_FILE = os.path.join(LOG_DIR, f"run_{RUN_ID}.log")
    setup_logging(force=True)
    return RUN_ID


def run_task(topic: str, skill_mode: str = "lazy",
             enable_files: bool = False,
             temperature: float | None = None,
             max_tokens: int | None = None,
             workspace: str | None = None,
             enable_shell: bool = False) -> dict:
    """执行一次「自建 skill_loader」实现，返回汇总（含简报正文与产物路径）。

    同时供命令行 ``main()`` 与 Streamlit UI 调用，因此不读取 sys.argv。
    ``enable_files=True`` 时启用 workspace 文件读写：
    运行前把 ``<workspace>/sources/`` 读入作为补充材料，运行后把简报写入 ``<workspace>/briefs/``。
    ``temperature`` / ``max_tokens`` 为 None 时沿用当前值；
    ``workspace`` 为 None 时恢复默认的项目内 ``workspace/`` 目录。
    """
    global REGISTRY, STATS
    STATS = fresh_stats()
    if temperature is not None:
        set_temperature(temperature)
    if max_tokens is not None:
        set_max_tokens(max_tokens)
    run_id = start_run()
    REGISTRY = None

    # workspace：每次运行都重置一次，避免上一轮的自定义目录残留
    ws_path = None
    if FILES_AVAILABLE:
        try:
            ws_path = use_workspace(workspace)
            if workspace:
                log.info("workspace | 使用自定义目录: %s", ws_path)
        except Exception as e:                      # FileToolError / ValueError
            code = getattr(e, "code", type(e).__name__)
            msg = getattr(e, "message", str(e))
            log.warning("workspace | 拒绝使用 %r [%s] %s，已回退默认目录", workspace, code, msg)
            STATS["errors"].append(f"workspace 被拒绝[{code}]: {msg}")
            ws_path = use_workspace(None)

    # 文件读写：读入 workspace/sources 作为补充材料（仅在启用时）
    source_text, source_files = "", []
    if enable_files and FILES_AVAILABLE:
        try:
            collected = collect_sources()
            source_text = collected.get("text", "")
            source_files = collected.get("files", [])
            STATS["tool_calls"]["read_file"] = len(source_files)
            if source_files:
                log.info("文件读写 | 读入补充材料 %d 个: %s（合计 %d 字符）",
                         len(source_files), source_files, len(source_text))
            else:
                log.info("文件读写 | workspace/sources 为空，本次无补充材料")
        except Exception as e:
            log.warning("文件读写 | 读取 sources 失败: %s", e)
            STATS["errors"].append(f"文件读取失败: {type(e).__name__}: {e}")
    elif enable_files and not FILES_AVAILABLE:
        log.warning("文件读写 | file_tools 不可用，已忽略该选项")

    # 技能层第一级：启动时只扫描元信息（frontmatter），技能正文不驻留上下文
    if SKILLS_AVAILABLE:
        REGISTRY = SkillRegistry(mode=skill_mode).scan()
        if skill_mode == "eager":
            REGISTRY.load_all()              # 对照组：启动时装入全部技能正文
        _sk = REGISTRY.stats()
        log.info("技能层 mode=%s | 技能 %d 个 | 目录 %d token | 扫描 %.2fms%s",
                 REGISTRY.mode, _sk["skill_count"], _sk["catalog_tokens"], _sk["scan_ms"],
                 f" | 预加载全部正文 {_sk['all_body_tokens']} token / {_sk['preload_ms']}ms"
                 if skill_mode == "eager" else "")
    else:
        log.warning("技能层不可用（skill_loader 未导入），等同 --skills=off")

    log.info("========== LangGraph 统一任务实测开始 ==========")
_mt = current_max_tokens()
    log.info("主题=%s | 模型=%s | 温度=%.2f | 输出上限=%s | 技能模式=%s | "
             "实现=skill_loader | RUN_ID=%s",
             topic, os.environ.get("DEEPSEEK_MODEL"), current_temperature(),
             f"{_mt}" if _mt is not None else "不限", skill_mode, run_id)
    log.info("工具集=%s | workspace 文件读写=%s | workspace 目录=%s | shell 命令=%s",
             [t.name for t in get_tools(enable_files, enable_shell)],
             ("启用（补充材料 %d 个）" % len(source_files)) if enable_files else "关闭",
             _rel(str(ws_path)) if ws_path else "默认",
             "启用" if enable_shell else "关闭")

    # 用计算器工具做一次演示调用（工具调用稳定性 + 参数校验测试）
    log.info("演示 calculator: 2**10 + 3*7 = %s",
             calculator.invoke({"expression": "2**10 + 3*7"}))

    # shell 命令演示：固定流水线的节点不会临时决定调工具，这里只做连通性与
    # 安全逻辑（白名单 / 超时 / 输出截断）验证，不代表模型能自主执行命令。
    shell_demo = {}
    if enable_shell and SHELL_AVAILABLE:
        shell_demo = shell_impl("git --version")
        log.info("演示 shell: git --version → rc=%s, %s | 白名单: %s",
                 shell_demo.get("returncode"),
                 (shell_demo.get("stdout") or shell_demo.get("message") or "").strip()[:60],
                 allowed_commands_text())
        STATS["tool_calls"]["shell"] = 1

    app = build_graph()
    t0 = time.time()
    try:
        final = app.invoke({"topic": topic, "source_material": source_text})
        total = round(time.time() - t0, 2)
    except Exception as e:
        log.exception("图执行失败")
        total = round(time.time() - t0, 2)
        STATS["errors"].append(f"图执行异常: {type(e).__name__}: {e}")
        final = {"brief": "", "errors": []}

    # 汇总统计
    all_errors = list(final.get("errors", [])) + STATS["errors"]
    summary = {
        "run_id": run_id,
        "impl": "skill_loader",
        "topic": topic,
        "total_elapsed_s": total,
        "node_timings": STATS["node_timings"],
        "tool_calls": STATS["tool_calls"],
        "search_backend": STATS["search_backend"],
        "fetch_backend": STATS["fetch_backend"],
        "skills": (REGISTRY.stats() if REGISTRY else {}),
        "skills_used": list(final.get("skills_used", [])),
        "llm_calls": STATS["llm_calls"],
        "token_input": STATS["token_input"],
        "token_output": STATS["token_output"],
        "token_total": STATS["token_input"] + STATS["token_output"],
        "file_tools": {"enabled": bool(enable_files and FILES_AVAILABLE),
                       "sources": source_files,
                       "written": [],
                       "workspace": _rel(str(ws_path)) if ws_path else ""},
        "shell_tools": {"enabled": bool(enable_shell and SHELL_AVAILABLE),
                        "whitelist": (allowed_commands_text()
                                      if (enable_shell and SHELL_AVAILABLE) else ""),
                        "demo": {k: shell_demo.get(k) for k in
                                 ("ok", "returncode", "stdout", "duration_s")
                                 if k in shell_demo}},
        "temperature": current_temperature(),
        "max_tokens": current_max_tokens(),
        "errors": all_errors,
    }
    log.info("总耗时 %.2fs | LLM调用 %d 次 | token(in=%d, out=%d, 总=%d)",
             total, STATS["llm_calls"], STATS["token_input"],
             STATS["token_output"], STATS["token_input"] + STATS["token_output"])
    _log_timing_table(total)
    if REGISTRY:
        _sk = REGISTRY.stats()
        log.info("技能层汇总: mode=%s | 加载 %d/%d 个技能 | 注入正文 %d token | 目录 %d token",
                 _sk["mode"], _sk["loaded_count"], _sk["skill_count"],
                 _sk["loaded_body_tokens"], _sk["catalog_tokens"])
    if all_errors:
        log.warning("故障记录: %s", all_errors)

    # 保存最终简报
    brief_text = final.get("brief", "")
    brief_path = os.path.join(OUT_DIR, f"brief_{run_id}.md")
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(brief_text)

    # 文件读写：把简报写入 <workspace>/briefs/（演示 write_file，默认不覆盖）
    if enable_files and FILES_AVAILABLE and brief_text.strip():
        rel_out = f"briefs/brief_{run_id}.md"
        try:
            res = write_file_impl(rel_out, brief_text, overwrite=False)
            summary["file_tools"]["written"].append(res["path"])
            STATS["tool_calls"]["write_file"] = 1
            log.info("文件读写 | 已写入 %s/%s（%d 字节，sha256=%s）",
                     _rel(str(ws_path)) if ws_path else "workspace",
                     res["path"], res["bytes_written"], res["sha256"][:12])
        except Exception as e:                      # noqa: BLE001 - 写入失败不影响主流程
            code = getattr(e, "code", type(e).__name__)
            msg = getattr(e, "message", str(e))
            log.warning("文件读写 | 写入失败 [%s] %s", code, msg)
            STATS["errors"].append(f"文件写入失败[{code}]: {msg}")

    # 保存统计
    stat_path = os.path.join(OUT_DIR, f"stats_{run_id}.json")
    with open(stat_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    log.info("简报已保存: %s", _rel(brief_path))
    log.info("统计已保存: %s", _rel(stat_path))
    log.info("========== 实测结束 ==========")

    return {
        **summary,
        "brief": brief_text,
        "brief_path": _rel(brief_path),
        "stats_path": _rel(stat_path),
        "log_path": _rel(LOG_FILE),
    }


def main() -> None:
    (topic, skill_mode, enable_files, temperature,
     max_tokens, workspace, enable_shell) = _parse_args()
    summary = run_task(topic, skill_mode, enable_files=enable_files,
                       temperature=temperature, max_tokens=max_tokens,
                       workspace=workspace, enable_shell=enable_shell)
    print("\n===== 最终简报 =====\n")
    print(summary.get("brief", ""))
    print(f"\n采样温度: {summary.get('temperature')} ｜ "
          f"输出上限: {summary.get('max_tokens') if summary.get('max_tokens') else '不限'}")
    ft = summary.get("file_tools") or {}
    if ft.get("enabled"):
        print("\n===== workspace 文件读写 =====")
        print(f"  目录        : {ft.get('workspace') or '（默认）'}")
        print(f"  读入补充材料: {ft.get('sources') or '无'}")
        print(f"  写入简报    : {ft.get('written') or '无'}")
    st = summary.get("shell_tools") or {}
    if st.get("enabled"):
        print("\n===== shell 命令（只读白名单） =====")
        print(f"  允许的命令  : {st.get('whitelist')}")
        demo = st.get("demo") or {}
        print(f"  演示结果    : rc={demo.get('returncode')} "
              f"{(demo.get('stdout') or '').strip()[:50]}")


if __name__ == "__main__":
    main()
