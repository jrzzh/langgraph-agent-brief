# -*- coding: utf-8 -*-
"""
冒烟测试：环境导入 / 模型连通性 / 工具后端可用性
说明：
- 工具主后端为千问 MCP（EnhancedSearch 搜索 + WebFetch 抓取），Bing 与 requests+lxml 为降级实现
- 版本用 importlib.metadata 读取（langgraph 等包不暴露 __version__，旧写法会显示 unknown）
- 未配置 DASHSCOPE_API_KEY 时，MCP 检查会提示"将降级"，主流程仍可运行
"""
import importlib.metadata as md
import json
import os
import time

from dotenv import load_dotenv

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MCP_BASE = "https://maas.qianwenaiapi.com/api/v1/mcps"


def ver(pkg: str) -> str:
    try:
        return md.version(pkg)
    except Exception:
        return "未安装"


def main() -> None:
    load_dotenv()
    results = {}

    # 1. 关键包导入与真实版本
    try:
        import langchain_core  # noqa: F401
        import langchain_mcp_adapters  # noqa: F401
        import langchain_openai  # noqa: F401
        import langgraph  # noqa: F401

        results["import"] = "OK"
        results["versions"] = {
            p: ver(p) for p in [
                "langgraph", "langchain", "langchain-core", "langchain-openai",
                "langchain-community", "langchain-mcp-adapters", "mcp",
                "requests", "lxml",
            ]
        }
    except Exception as e:
        results["import"] = f"FAIL: {e!r}"

    # 2. DeepSeek 连通性（1 次真实调用 + token 统计）
    try:
        from langchain_openai import ChatOpenAI

        t0 = time.time()
        llm = ChatOpenAI(
            model=os.environ["DEEPSEEK_MODEL"],
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ["DEEPSEEK_BASE_URL"],
            temperature=0,
            timeout=60,
        )
        resp = llm.invoke("用一句话回答：你是谁？")
        results["deepseek"] = {
            "ok": True,
            "model": os.environ["DEEPSEEK_MODEL"],
            "reply": resp.content[:80],
            "elapsed_s": round(time.time() - t0, 2),
            "usage": getattr(resp, "usage_metadata", None),
        }
    except Exception as e:
        results["deepseek"] = {"ok": False, "error": repr(e)}

    # 3. 千问 MCP（联网搜索 / 网页抓取的主后端）：工具自动发现
    try:
        import asyncio

        from langchain_mcp_adapters.client import MultiServerMCPClient

        key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not key:
            results["qwen_mcp"] = {"ok": False,
                                   "note": "未配置 DASHSCOPE_API_KEY，工具将降级为本地实现"}
        else:
            async def _probe():
                auth = {"Authorization": f"Bearer {key}"}
                client = MultiServerMCPClient({
                    "qwen-search": {"transport": "streamable_http",
                                    "url": f"{MCP_BASE}/EnhancedSearch/mcp", "headers": auth},
                    "qwen-fetch": {"transport": "streamable_http",
                                   "url": f"{MCP_BASE}/WebFetch/mcp", "headers": auth},
                })
                return await client.get_tools()

            t0 = time.time()
            tools = asyncio.run(_probe())
            results["qwen_mcp"] = {
                "ok": True,
                "tools": sorted(t.name for t in tools),
                "elapsed_s": round(time.time() - t0, 2),
            }
    except Exception as e:
        results["qwen_mcp"] = {"ok": False, "error": repr(e)[:200],
                               "note": "MCP 不可用时主流程会自动降级本地实现"}

    # 4. 降级后端：Bing 检索可用性
    try:
        import requests
        from lxml import html

        t0 = time.time()
        r = requests.get("https://cn.bing.com/search",
                         params={"q": "LLM context engineering"},
                         headers={"User-Agent": UA}, timeout=15)
        n = len(html.fromstring(r.text).xpath('//li[contains(@class, "b_algo")]'))
        results["bing_search"] = {
            "ok": r.status_code == 200 and n > 0, "http": r.status_code,
            "results": n, "elapsed_s": round(time.time() - t0, 2),
        }
    except Exception as e:
        results["bing_search"] = {"ok": False, "error": repr(e)}

    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
