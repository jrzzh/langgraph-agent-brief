"""内置工具的自包含实现。

与「外部注入」的关系：这些工具**不依赖 MCP、不依赖任何业务模块**，用的是
公开 HTTP + 标准库，所以通用 UI 可以不配任何 MCP 服务器就先跑起来；用户配了
MCP 服务器时，那些工具由宿主另行注入，与本模块并列。

请注意分层：``agent.py`` 只有循环本体，本模块是可选的伴生件——
不 import 它就完全不会拉进 requests / lxml。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests
from langchain_core.tools import tool
from lxml import html

#: 抓取时的 UA，避免被站点按"脚本"直接拒绝
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HTTP_TIMEOUT = float(os.environ.get("LANGGRAPH_AGENT_HTTP_TIMEOUT", "15"))
MAX_TEXT_CHARS = 8000

#: 文件工具只允许这些后缀（与案例层保持一致，避免两套规则）
TEXT_SUFFIXES = frozenset(
    {".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".csv", ".log",
     ".py", ".js", ".ts", ".html", ".htm", ".xml", ".toml", ".ini", ".rst"}
)
WRITE_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".json", ".csv", ".log", ".py"})


def _strip_xml_decl(text: str) -> str:
    """lxml 不接受带 encoding 声明的 str，先去掉 XML 声明。"""
    head = text.lstrip()[:64].lower()
    if head.startswith("<?xml"):
        end = text.find("?>")
        if end != -1:
            return text[end + 2:]
    return text


def _squash(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# 三个基线工具
# ---------------------------------------------------------------------------
@tool("search_web", description="搜索公开网页并返回候选来源（标题、链接、摘要）。")
def search_web(query: str, limit: int = 5) -> str:
    """用 Bing 的公开结果页做检索，返回 JSON 数组。"""
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "setlang": "zh-Hans"},
            headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        doc = html.fromstring(_strip_xml_decl(resp.text))
    except Exception as exc:                       # noqa: BLE001
        return f"[检索失败: {type(exc).__name__}: {exc}]"

    out: list[dict[str, str]] = []
    for item in doc.xpath("//li[contains(@class,'b_algo')]"):
        link = item.xpath(".//h2/a")
        if not link:
            continue
        a = link[0]
        out.append({
            "title": _squash(a.text_content()),
            "url": a.get("href", ""),
            "snippet": _squash(" ".join(p.text_content() for p in item.xpath(".//p")))[:300],
        })
        if len(out) >= max(1, int(limit)):
            break
    if not out:
        return "[检索失败: 没有解析到结果，可能是页面结构变化或被拦截]"
    return json.dumps(out, ensure_ascii=False)


@tool("read_webpage", description="读取公开网页正文，返回纯文本。")
def read_webpage(url: str) -> str:
    """取正文文本；脚本、样式、导航一律丢掉。"""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        doc = html.fromstring(_strip_xml_decl(resp.text))
    except Exception as exc:                       # noqa: BLE001
        return f"[读取失败: {type(exc).__name__}: {exc}]"

    for bad in doc.xpath("//script|//style|//nav|//footer|//header|//aside"):
        bad.getparent().remove(bad)
    text = _squash(doc.text_content())
    return text[:MAX_TEXT_CHARS] if text else "[读取失败: 正文为空]"


@tool("calculator", description="执行受控算术表达式，只支持数值与 + - * / ** % 括号。")
def calculator(expression: str) -> str:
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
               ast.USub, ast.UAdd)
    try:
        tree = ast.parse(expression, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, allowed):
                return f"错误：不允许的操作 {type(node).__name__}"
        return str(eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {}))
    except Exception as exc:                       # noqa: BLE001
        return f"计算错误：{exc}"


# ---------------------------------------------------------------------------
# 文件工具（按调用方给的根目录绑定，不做全局状态）
# ---------------------------------------------------------------------------
def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _make_file_tools(root: Path | None, read_paths: Sequence[Path] = ()):
    """把三个文件工具绑定到指定根目录。没有根目录时返回空。"""

    allowed_roots = [p.resolve() for p in read_paths if p]
    if root is not None:
        allowed_roots.insert(0, root.resolve())

    def _resolve(raw: str, *, write: bool = False) -> Path | None:
        target = Path(raw)
        if not target.is_absolute():
            base = root.resolve() if root else (allowed_roots[0] if allowed_roots else None)
            if base is None:
                return None
            target = base / target
        target = target.resolve()
        if not any(_inside(r, target) for r in allowed_roots):
            return None
        # 写操作额外收紧：只能落在工作目录内，附件目录只读
        if write and (root is None or not _inside(root.resolve(), target)):
            return None
        return target

    @tool("read_file", description="读取当前工作空间或本轮附件里的文件。")
    def read_file(path: str) -> str:
        target = _resolve(path)
        if target is None:
            return "[读取失败: 路径不在可读范围内]"
        if not target.is_file():
            return f"[读取失败: 不是文件 {path}]"
        if target.suffix.lower() not in TEXT_SUFFIXES:
            return f"[读取失败: 不支持的后缀 {target.suffix}]"
        if target.stat().st_size > 2_000_000:
            return "[读取失败: 文件超过 2 MB]"
        return target.read_text(encoding="utf-8", errors="replace")

    @tool("write_file", description="在当前工作空间内写入文件。")
    def write_file(path: str, content: str) -> str:
        if root is None:
            return "[写入失败: 本轮没有指定工作目录]"
        target = _resolve(path, write=True)
        if target is None:
            return "[写入失败: 路径不在工作目录内]"
        if target.suffix.lower() not in WRITE_SUFFIXES:
            return f"[写入失败: 不允许写 {target.suffix}]"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
        return f"[已写入 {target}（{len(content)} 字）]"

    @tool("list_directory", description="列出当前工作空间或附件目录的内容。")
    def list_directory(path: str = ".") -> str:
        target = _resolve(path)
        if target is None or not target.is_dir():
            return "[列目录失败: 路径不在可读范围内]"
        rows = []
        for entry in sorted(target.iterdir())[:200]:
            rows.append(f"{'[D]' if entry.is_dir() else '[F]'} {entry.name}")
        return "\n".join(rows) or "[空目录]"

    return {"read_file": read_file, "write_file": write_file, "list_directory": list_directory}


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------
_BASELINE = {"search_web": search_web, "read_webpage": read_webpage, "calculator": calculator}


def build_tools(
    names: Iterable[str] | None = None,
    *,
    workspace: str | os.PathLike | None = None,
    read_paths: Sequence[str | os.PathLike] = (),
) -> list[Any]:
    """按名字挑出工具对象。

    ``names`` 为 ``None`` 表示全给；给了 workspace / read_paths 才有文件工具。
    未知名字直接忽略（UI 可能残留旧开关），不会因此崩掉整轮。
    """
    wanted = set(names) if names is not None else None
    root = Path(workspace).expanduser() if workspace else None
    file_tools = _make_file_tools(root, tuple(Path(p) for p in read_paths))

    picked: list[Any] = []
    for name, item in _BASELINE.items():
        if wanted is None or name in wanted:
            picked.append(item)
    for name, item in file_tools.items():
        if wanted is not None and name not in wanted:
            continue
        spec_requires_root = name == "write_file"
        if spec_requires_root and root is None:
            continue
        if root is None and not read_paths:
            continue
        picked.append(item)
    return picked


__all__ = [
    "MAX_TEXT_CHARS",
    "TEXT_SUFFIXES",
    "USER_AGENT",
    "WRITE_SUFFIXES",
    "build_tools",
    "calculator",
    "read_webpage",
    "search_web",
]