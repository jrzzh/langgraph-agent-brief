"""千问 MCP 工具源（本案例的外部注入源之一）。

这个模块只做一件事：把「联网检索」和「网页抓取」包装成 LangChain 工具，
供通用 agent 内核通过 ``tools=`` 注入。内核本身不 import 这里面的任何东西。

**两级降级**：千问 MCP（EnhancedSearch / WebFetch）失败时，自动回退到
本地实现（Bing HTML 检索 / requests + lxml 抓取），所以没有 ``DASHSCOPE_API_KEY``
也能跑通，只是检索质量下降。

**统计与日志由调用方注入**（:func:`configure`）：本模块不依赖 agent_brief，
避免 ``agent_brief → mcps.qwen_mcp → agent_brief`` 的循环导入。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time

import requests
from langchain_core.tools import tool
from lxml import html

#: 默认日志器（调用方未注入时使用）
_log = logging.getLogger("langgraph_brief.mcp")

#: 默认统计容器（调用方未注入时使用，结构与 agent_brief.STATS 对齐）
_stats: dict = {
    "search_backend": {"qwen_mcp": 0, "bing_fallback": 0, "cache_hit": 0},
    "fetch_backend": {"qwen_mcp": 0, "local_fallback": 0, "cache_hit": 0},
    "errors": [],
}


def configure(*, stats: dict | None = None, logger: logging.Logger | None = None) -> None:
    """由调用方注入统计容器与日志器。

    ``stats`` 需要提供 ``search_backend`` / ``fetch_backend`` / ``errors`` 三个键；
    ``logger`` 一般就是应用主模块的 logger。
    """
    global _stats, _log
    if stats is not None:
        _stats = stats
    if logger is not None:
        _log = logger

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

#: 单次 MCP 调用的硬性上限（秒）。千问 WebFetch 遇到慢站会一直挂在 SSE 上，
#: 实测出现过单次 75 秒——界面因此长时间空白。超过这个时间就抛超时，走本地降级。
MCP_CALL_TIMEOUT = float(os.environ.get("LANGGRAPH_BRIEF_MCP_TIMEOUT", "10"))


async def _ainvoke_with_deadline(tool_obj, args: dict, timeout: float):
    """带截止时间的 MCP 工具调用。"""
    return await asyncio.wait_for(tool_obj.ainvoke(args), timeout=timeout)


async def _load_qwen_tools() -> dict:
    """经 langchain-mcp-adapters 接入千问 MCP 服务（Streamable HTTP）。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY")
    auth = {"Authorization": f"Bearer {key}"}
    # timeout / sse_read_timeout 都显式给死：默认值偏长，慢站会把整轮拖住
    client = MultiServerMCPClient({
        "qwen-search": {"transport": "streamable_http",
                        "url": f"{QWEN_MCP_BASE}/EnhancedSearch/mcp",
                        "headers": auth,
                        "timeout": MCP_CALL_TIMEOUT,
                        "sse_read_timeout": MCP_CALL_TIMEOUT},
        "qwen-fetch": {"transport": "streamable_http",
                       "url": f"{QWEN_MCP_BASE}/WebFetch/mcp",
                       "headers": auth,
                       "timeout": MCP_CALL_TIMEOUT,
                       "sse_read_timeout": MCP_CALL_TIMEOUT},
    })
    return {t.name: t for t in await client.get_tools()}


def _ensure_qwen_tools() -> dict:
    """按需加载并缓存千问 MCP 工具（双检锁，避免并发重复建会话）。"""
    if not _qwen_tools:
        with _qwen_lock:
            if not _qwen_tools:
                t0 = time.time()
                _qwen_tools.update(asyncio.run(_load_qwen_tools()))
                _log.info("千问 MCP 工具加载完成: %s (%.2fs)",
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
    text = _blocks_to_text(
        asyncio.run(_ainvoke_with_deadline(tool_obj, {"query": query}, MCP_CALL_TIMEOUT))
    )
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
    data = json.loads(_blocks_to_text(
        asyncio.run(_ainvoke_with_deadline(tool_obj, {"url": url}, MCP_CALL_TIMEOUT))
    ))
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


#: 进程内结果缓存：(kind, key) -> (写入时间, 值)。
#: 目的是**同主题重跑**时不必再等一遍检索与抓取——演示时反复跑同一个题很常见。
CACHE_TTL = float(os.environ.get("LANGGRAPH_BRIEF_CACHE_TTL", "300"))
_cache: dict[tuple[str, str], tuple[float, str]] = {}


def _cache_get(kind: str, key: str) -> str | None:
    if CACHE_TTL <= 0:
        return None
    hit = _cache.get((kind, key))
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


#: 工具失败时返回的错误文本前缀。**不能**用"以 [ 开头"判断：
#: 搜索结果本身就是 JSON 数组（``[{...}]``），那样会把正常结果也拒掉。
_ERROR_PREFIXES = ("[检索失败", "[读取失败")


def _cache_put(kind: str, key: str, value: str) -> None:
    """只缓存成功结果：错误文本不缓存，否则一次网络抖动会污染后续运行。"""
    if CACHE_TTL > 0 and value and not value.lstrip().startswith(_ERROR_PREFIXES):
        _cache[(kind, key)] = (time.time(), value)


@tool
def web_search(query: str) -> str:
    """联网搜索指定关键词，返回多条结果的标题、链接与摘要（JSON 数组）。
    主后端：千问 EnhancedSearch MCP（联网搜索增强版）；失败重试 1 次后降级 Bing。"""
    cached = _cache_get("search", query)
    if cached is not None:
        _stats["search_backend"]["cache_hit"] = _stats["search_backend"].get("cache_hit", 0) + 1
        return cached
    last = None
    for attempt in (1, 2):
        try:
            out = _qwen_search(query)
            _stats["search_backend"]["qwen_mcp"] += 1
            _cache_put("search", query, out)
            return out
        except Exception as e:
            last = e
            if attempt == 1:
                _log.warning("千问 EnhancedSearch 第 1 次失败(%s)，重试…", _root_cause(e))
                time.sleep(1.0)
    _stats["search_backend"]["bing_fallback"] += 1
    msg = f"千问 EnhancedSearch 失败({_root_cause(last)})，已降级 Bing"
    _log.warning(msg)
    _stats["errors"].append(msg)
    # 工具**绝不抛异常**：模型直接调用的场景（如 deepagents）里，一次 ETL/网络失败
    # 会直接把整轮打断；返回错误文本让模型自行换策略才是正确姿势。
    try:
        return _bing_search(query)
    except Exception as e:                          # noqa: BLE001
        reason = _root_cause(e)
        _stats["errors"].append(f"Bing 降级也失败: {reason}")
        _log.warning("Bing 降级也失败: %s", reason)
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
    主后端：千问 MCP web_fetch（标准化 Markdown）；失败重试 1 次后降级本地抓取。

    每次 MCP 调用有硬性上限（``MCP_CALL_TIMEOUT``，默认 10 秒），超时即降级本地抓取，
    避免慢站把整轮拖到一分钟以上。"""
    cached = _cache_get("fetch", url)
    if cached is not None:
        _stats["fetch_backend"]["cache_hit"] = _stats["fetch_backend"].get("cache_hit", 0) + 1
        return cached
    last = None
    for attempt in (1, 2):
        try:
            out = _qwen_fetch(url)[:3000]
            _stats["fetch_backend"]["qwen_mcp"] += 1
            _cache_put("fetch", url, out)
            return out
        except Exception as e:
            last = e
            if attempt == 1:
                _log.warning("千问 web_fetch 第 1 次失败(%s)，重试…", _root_cause(e))
                time.sleep(0.5)                     # 原来 1s，缩短一半
    _stats["fetch_backend"]["local_fallback"] += 1
    msg = f"千问 web_fetch 失败({_root_cause(last)})，已降级本地抓取: {url}"
    _log.warning(msg)
    _stats["errors"].append(msg)
    # 同上：本地抓取失败（404/429/超时/解析失败）也不能抛出去
    try:
        out = _http_fetch(url)
        _cache_put("fetch", url, out)
        return out
    except Exception as e:                          # noqa: BLE001
        reason = _root_cause(e)
        _stats["errors"].append(f"本地抓取也失败({url}): {reason}")
        _log.warning("本地抓取也失败 %s: %s", url, reason)
        return f"[读取失败: {reason}]"
