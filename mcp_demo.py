# -*- coding: utf-8 -*-
"""
MCP 接入演示：LangGraph + langchain-mcp-adapters
================================================================
验证 LangGraph 通过 langchain-mcp-adapters 接入外部 MCP Server：
1) 以 stdio 方式拉起本地 FastMCP Server（mcp_server.py）
2) MultiServerMCPClient 自动发现并加载其工具（转换为 LangChain 工具）
3) 把 MCP 工具作为一个“校验节点”接入 LangGraph 状态图
================================================================
"""
import asyncio
import json
import os
import sys
import time

from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph import END, START, StateGraph
from typing import Annotated, TypedDict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class CheckState(TypedDict):
    text: str
    report: str


async def demo() -> dict:
    # 使用 streamable-http 传输（stdio 在受限沙箱下依赖命名管道会失败）
    client = MultiServerMCPClient({
        "brief-analyzer": {
            "transport": "streamable-http",
            "url": "http://127.0.0.1:8000/mcp",
        }
    })

    t0 = time.time()
    tools = await client.get_tools()
    load_s = round(time.time() - t0, 2)
    by_name = {t.name: t for t in tools}

    result = {
        "工具发现数量": len(tools),
        "工具名称": [t.name for t in tools],
        "工具加载耗时_s": load_s,
        "工具描述": {t.name: (t.description or "")[:60] for t in tools},
    }

    # 把 MCP 工具作为 LangGraph 校验节点（MCP 工具为异步，需 ainvoke）
    async def check_node(state: CheckState) -> dict:
        text = state["text"]
        wc = await by_name["word_count"].ainvoke({"text": text})
        hd = await by_name["md_heading_count"].ainvoke({"text": text})
        cf = await by_name["char_frequency"].ainvoke({"text": text, "char": "#"})
        return {"report": json.dumps(
            {"字数(去空白)": wc, "标题数": hd, "'#'出现次数": cf},
            ensure_ascii=False)}

    g = StateGraph(CheckState)
    g.add_node("check", check_node)
    g.add_edge(START, "check")
    g.add_edge("check", END)
    app = g.compile()

    sample = ("# 简报\n## 正文\n这是一段测试文本，共用于校验 MCP 工具。\n## 来源\n- [a](http://x)")
    out = await app.ainvoke({"text": sample})
    result["图节点调用结果"] = out["report"]

    return result


if __name__ == "__main__":
    try:
        r = asyncio.run(demo())
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"错误": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False, indent=2))
