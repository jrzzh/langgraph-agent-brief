# -*- coding: utf-8 -*-
"""
LangGraph 统一任务实测脚本
================================================================
框架    : LangGraph 1.2.16  +  LangChain 1.4.6
模型    : DeepSeek (deepseek-v4-pro)
统一任务: 联网检索资料 → 提取关键信息 → 事实交叉核验 → 生成 500 字以内
          带来源链接的 Markdown 技术简报
统一工具: 联网搜索(Bing) + 网页内容读取 + 计算器
================================================================
用法    : python agent_brief.py [主题]
示例    : python agent_brief.py 大模型上下文工程
"""

import ast
import json
import logging
import os
import re
import sys
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

# ---------------------------------------------------------------------------
# 日志配置（stdout + 文件，UTF-8）
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(LOG_DIR, f"run_{RUN_ID}.log")


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("langgraph")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    # 文件
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # 控制台
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


log = setup_logging()

# ---------------------------------------------------------------------------
# 全局统计（token 消耗、各节点耗时、故障）
# ---------------------------------------------------------------------------
STATS = {
    "token_input": 0,
    "token_output": 0,
    "llm_calls": 0,
    "node_timings": {},
    "tool_calls": {},
    "errors": [],
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


@tool
def web_search(query: str) -> str:
    """联网搜索指定关键词，返回多条结果的标题、链接与摘要（JSON 数组）。
    检索后端：Bing（cn.bing.com）。"""
    url = "https://cn.bing.com/search"
    params = {"q": query, "count": 10}
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        tree = html.fromstring(r.text)  # r.text 已按 utf-8 正确解码
        results = []
        for li in tree.xpath('//li[contains(@class, "b_algo")]'):
            a = li.xpath('.//h2/a')
            if not a:
                continue
            title = a[0].text_content().strip()
            href = a[0].get("href", "")
            p = li.xpath('.//p')
            snippet = p[0].text_content().strip() if p else ""
            results.append({"title": title, "url": href, "snippet": snippet})
        if not results:
            return "[]"
        return json.dumps(results[:5], ensure_ascii=False)
    except Exception as e:  # 记录故障，返回空结果由上层降级
        log.warning("web_search 失败 query=%s err=%s", query, e)
        raise


@tool
def web_fetch(url: str) -> str:
    """读取指定网页并提取正文纯文本（截取前 3000 字符）。"""
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
    text = re.sub(r"\s+", " ", text).strip()
    return text[:3000]


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
    resp = llm.invoke(prompt)
    record_usage(resp)
    STATS["node_timings"]["verify"] = round(time.time() - t0, 2)
    log.info("verify 完成: 核验结论 %d 字符", len(resp.content))
    return {"facts": resp.content}


def brief_node(state: BriefState) -> dict:
    """节点5：生成 500 字以内、带来源链接的 Markdown 技术简报。"""
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
    resp = llm.invoke(prompt)
    record_usage(resp)
    brief = resp.content

    # 字数自检：去掉 Markdown 语法、链接保留文字、去空白后统计
    def _count(text: str) -> int:
        body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
        body = re.sub(r"[#*>`\-|]", "", body)
        body = re.sub(r"\s+", "", body)
        return len(body)

    n = _count(brief)
    if n > 500:
        log.warning("简报正文字数 %d 超限，触发压缩重试", n)
        STATS["errors"].append(f"简报首次生成超字数({n}字)，已触发压缩")
        p2 = (f"以下简报正文字数 {n} 字，超过 500 字限制。请压缩到 500 字以内，"
              f"保持“来源”小节与链接不变：\n\n{brief}")
        resp2 = llm.invoke(p2)
        record_usage(resp2)
        brief = resp2.content
        log.info("压缩后正文字数: %d", _count(brief))

    STATS["node_timings"]["brief"] = round(time.time() - t0, 2)
    log.info("brief 完成: %d 字符(正文 %d)", len(brief), _count(brief))
    return {"brief": brief}


# ---------------------------------------------------------------------------
# 组装状态图
# ---------------------------------------------------------------------------
def build_graph():
    g = StateGraph(BriefState)
    g.add_node("plan", plan_node)
    g.add_node("search", search_node)
    g.add_node("fetch", fetch_node)
    g.add_node("verify", verify_node)
    g.add_node("brief", brief_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "search")
    g.add_edge("search", "fetch")
    g.add_edge("fetch", "verify")
    g.add_edge("verify", "brief")
    g.add_edge("brief", END)
    return g.compile()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    topic = sys.argv[1] if len(sys.argv) > 1 else "大模型上下文工程"
    log.info("========== LangGraph 统一任务实测开始 ==========")
    log.info("主题=%s | 模型=%s | RUN_ID=%s", topic,
             os.environ.get("DEEPSEEK_MODEL"), RUN_ID)

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
        "llm_calls": STATS["llm_calls"],
        "token_input": STATS["token_input"],
        "token_output": STATS["token_output"],
        "token_total": STATS["token_input"] + STATS["token_output"],
        "errors": all_errors,
    }
    log.info("总耗时 %.2fs | LLM调用 %d 次 | token(in=%d, out=%d, 总=%d)",
             total, STATS["llm_calls"], STATS["token_input"],
             STATS["token_output"], STATS["token_input"] + STATS["token_output"])
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

    log.info("简报已保存: %s", brief_path)
    log.info("统计已保存: %s", stat_path)
    log.info("========== 实测结束 ==========")
    print("\n===== 最终简报 =====\n")
    print(final.get("brief", ""))


if __name__ == "__main__":
    main()
