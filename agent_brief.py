# -*- coding: utf-8 -*-
"""
LangGraph 统一任务实测脚本
================================================================
框架    : LangGraph 1.2.16  +  LangChain 1.4.6
模型    : DeepSeek (deepseek-v4-pro)
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
LOG_DIR = os.path.join(BASE_DIR, "logs")
OUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)


def _rel(path: str) -> str:
    """转成相对项目根目录的路径，避免日志中写入本机绝对路径（便于提交/分享）。"""
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:          # 跨盘符等无法求相对路径的情况
        return os.path.basename(path)

# ---------------------------------------------------------------------------
# 日志配置（stdout + 文件，UTF-8）
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(LOG_DIR, f"run_{RUN_ID}.log")


log = logging.getLogger("langgraph")
log.setLevel(logging.DEBUG)
_handlers_ready = False


def setup_logging() -> logging.Logger:
    """挂载文件与控制台 handler。

    延迟到 main() 中调用，避免"仅 import 模块"时产生 0 字节日志文件。
    """
    global _handlers_ready
    if _handlers_ready:
        return log
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")     # 文件
    fh.setFormatter(fmt)
    log.addHandler(fh)
    ch = logging.StreamHandler(sys.stdout)                   # 控制台
    ch.setFormatter(fmt)
    log.addHandler(ch)
    _handlers_ready = True
    return log

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


def _bing_search(query: str) -> str:
    """降级后端：Bing HTML 抓取。"""
    r = requests.get("https://cn.bing.com/search",
                     params={"q": query, "count": 10},
                     headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    tree = html.fromstring(r.text)
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
    return _bing_search(query)


def _http_fetch(url: str) -> str:
    """降级后端：requests + lxml 提取正文纯文本。"""
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15,
                     allow_redirects=True)
    r.raise_for_status()
    # 处理常见编码
    if r.encoding and r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding
    tree = html.fromstring(r.text)
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
    return _http_fetch(url)


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


TOOLS = [web_search, web_fetch, calculator]

# ---------------------------------------------------------------------------
# 图状态定义
# ---------------------------------------------------------------------------
class BriefState(TypedDict):
    topic: str
    queries: list[str]
    search_results: str          # JSON 字符串
    fetched: str                 # 拼接的网页正文
    facts: str                   # 交叉核验结论
    brief: str                   # 最终简报
    skills_used: list[str]       # 技能路由选中的技能名
    skill_instructions: str      # 注入后续节点的技能正文（渐进式披露的产物）
    errors: Annotated[list[str], operator.add]  # 故障累积


# ---------------------------------------------------------------------------
# LLM 实例
# ---------------------------------------------------------------------------
llm = ChatOpenAI(
    model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    temperature=0.3,
    timeout=60,
)

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
    log.info("plan 生成查询: %s", queries)
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
        log.info("skill_select 跳过（技能层未启用或无技能）")
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
    log.info("skill_select 选中 %s | 注入正文 %d 字符（mode=%s）",
             chosen, len(instr), REGISTRY.mode)
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
    log.info("search 完成: %d 个查询, 得到 %d 条结果", len(queries), len(all_items))
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
    log.info("fetch 完成: 读取 %d 个网页", len(urls))
    return {"fetched": "\n\n".join(chunks), "errors": errors}


def verify_node(state: BriefState) -> dict:
    """节点4：LLM 对关键事实做交叉核验。"""
    t0 = time.time()
    topic = state["topic"]
    results = state.get("search_results") or "[]"
    fetched = state.get("fetched") or ""
    prompt = (
        f"主题：“{topic}”。以下是检索到的资料摘要与网页正文，请提取 3-5 条关键事实，"
        f"并对每条事实标注“多来源一致 / 单一来源 / 需进一步核实”，说明核验依据。\n\n"
        f"【检索摘要】\n{results[:1500]}\n\n【网页正文摘录】\n{fetched[:1500]}"
    )
    skill_txt = state.get("skill_instructions") or ""      # 渐进式披露：注入已加载技能
    if skill_txt:
        prompt = f"【已加载技能】\n{skill_txt}\n\n{prompt}"
    resp = llm.invoke(prompt)
    record_usage(resp)
    STATS["node_timings"]["verify"] = round(time.time() - t0, 2)
    log.info("verify 完成: 核验结论 %d 字符", len(resp.content))
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
        f"请基于下面的核验事实，撰写一份关于“{topic}”的中文技术简报。\n"
        f"要求：\n1) 正文（不含“来源”小节与链接）严格不超过 500 个汉字，超长请删减；\n"
        f"2) 使用 Markdown，含标题与分点；\n"
        f"3) 末尾附“来源”小节，列出以下链接；\n4) 只陈述已核验的事实，不确定的标注“待核实”。\n\n"
        f"【核验事实】\n{facts}\n\n【可用来源链接】\n{srcs}"
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
        p2 = (f"以下简报的**正文**（不含“来源”小节）为 {cur} 字，需压缩到 400 字以内："
              f"只保留最重要的事实，删除修饰语、重复表述与次要例证；"
              f"“来源”小节及其中的链接必须原样保留。\n\n{brief}")
        resp2 = llm.invoke(p2)
        record_usage(resp2)
        brief = resp2.content

    _, src_txt = split_body_sources(brief)
    src_chars = len(re.sub(r"\s+", "", src_txt))
    log.info("简报定稿：正文 %d 字（≤500），来源 %d 字不计入，压缩 %d 次",
             count_body_chars(brief), src_chars, attempt)

    STATS["node_timings"]["brief"] = round(time.time() - t0, 2)
    log.info("brief 完成: 全文 %d 字符 | 正文 %d 字 | 来源 %d 字",
             len(brief), count_body_chars(brief), src_chars)
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
def _parse_args() -> tuple[str, str]:
    """解析命令行：位置参数为主题；`--skills=lazy|eager|off` 选择技能加载模式。"""
    topic, mode = "大模型上下文工程", "lazy"
    for a in sys.argv[1:]:
        if a.startswith("--skills="):
            mode = a.split("=", 1)[1].strip().lower()
        elif not a.startswith("-"):
            topic = a
    if mode not in ("lazy", "eager", "off"):
        mode = "lazy"
    return topic, mode


def main() -> None:
    global REGISTRY
    setup_logging()          # 延迟挂载日志 handler：仅 import 时不产生空日志文件
    topic, skill_mode = _parse_args()

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
    log.info("主题=%s | 模型=%s | 技能模式=%s | RUN_ID=%s", topic,
             os.environ.get("DEEPSEEK_MODEL"), skill_mode, RUN_ID)

    # 用计算器工具做一次演示调用（工具调用稳定性 + 参数校验测试）
    log.info("演示 calculator: 2**10 + 3*7 = %s",
             calculator.invoke({"expression": "2**10 + 3*7"}))

    app = build_graph()
    t0 = time.time()
    try:
        final = app.invoke({"topic": topic})
        total = round(time.time() - t0, 2)
    except Exception as e:
        log.exception("图执行失败")
        total = round(time.time() - t0, 2)
        STATS["errors"].append(f"图执行异常: {type(e).__name__}: {e}")
        final = {"brief": "", "errors": []}

    # 汇总统计
    all_errors = list(final.get("errors", [])) + STATS["errors"]
    summary = {
        "run_id": RUN_ID,
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
        "errors": all_errors,
    }
    log.info("总耗时 %.2fs | LLM调用 %d 次 | token(in=%d, out=%d, 总=%d)",
             total, STATS["llm_calls"], STATS["token_input"],
             STATS["token_output"], STATS["token_input"] + STATS["token_output"])
    if REGISTRY:
        _sk = REGISTRY.stats()
        log.info("技能层汇总: mode=%s | 加载 %d/%d 个技能 | 注入正文 %d token | 目录 %d token",
                 _sk["mode"], _sk["loaded_count"], _sk["skill_count"],
                 _sk["loaded_body_tokens"], _sk["catalog_tokens"])
    if all_errors:
        log.warning("故障记录: %s", all_errors)

    # 保存最终简报
    brief_path = os.path.join(OUT_DIR, f"brief_{RUN_ID}.md")
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(final.get("brief", ""))
    # 保存统计
    stat_path = os.path.join(OUT_DIR, f"stats_{RUN_ID}.json")
    with open(stat_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    log.info("简报已保存: %s", _rel(brief_path))
    log.info("统计已保存: %s", _rel(stat_path))
    log.info("========== 实测结束 ==========")
    print("\n===== 最终简报 =====\n")
    print(final.get("brief", ""))


if __name__ == "__main__":
    main()
