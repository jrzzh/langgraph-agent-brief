# -*- coding: utf-8 -*-
"""
冒烟测试：环境导入 / DeepSeek 连通性 / 联网检索与网页解析可用性
说明：
- 检索后端为 Bing（cn.bing.com），即本项目统一工具集中「联网搜索」的实现
- 版本用 importlib.metadata 读取（langgraph 等包不暴露 __version__，旧写法会显示 unknown）
"""
import importlib.metadata as md
import json
import os
import time

from dotenv import load_dotenv

load_dotenv()
results = {}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def ver(pkg: str) -> str:
    try:
        return md.version(pkg)
    except Exception:
        return "未安装"


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
        "reply": resp.content[:80],
        "elapsed_s": round(time.time() - t0, 2),
        "usage": getattr(resp, "usage_metadata", None),
    }
except Exception as e:
    results["deepseek"] = {"ok": False, "error": repr(e)}

# 3. 联网检索：Bing（统一工具集中 web_search 的实际后端）
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