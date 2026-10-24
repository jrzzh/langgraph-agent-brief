# -*- coding: utf-8 -*-
"""
本地 MCP Server 示例（FastMCP）
================================================
用途：演示 LangGraph 通过 langchain-mcp-adapters 接入外部 MCP Server 工具。
本 server 提供三个文本分析工具，用于对简报做字数与格式校验。
运行：由 mcp_demo.py 以 stdio 方式自动拉起，也可手动 `python mcp_server.py`。
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("brief-analyzer")


@mcp.tool()
def word_count(text: str) -> int:
    """统计文本去除空白后的字符数（近似中文“字数”）。"""
    return len("".join(text.split()))


@mcp.tool()
def char_frequency(text: str, char: str) -> int:
    """统计指定字符在文本中出现的次数。"""
    return text.count(char)


@mcp.tool()
def md_heading_count(text: str) -> int:
    """统计 Markdown 文本中一级/二级标题（# 开头行）的数量。"""
    return sum(1 for line in text.splitlines()
               if line.strip().startswith("#"))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", default="streamable-http",
                    choices=["stdio", "streamable-http"])
    a = ap.parse_args()
    # host/port 已在 FastMCP() 默认值中：127.0.0.1:8000，HTTP 路径 /mcp
    mcp.run(transport=a.transport)
