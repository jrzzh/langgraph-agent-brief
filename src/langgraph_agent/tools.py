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
import re
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
#: 合规的机器人 UA（带联系方式）。部分站点（维基百科）专门拒绝伪装成浏览器的
#: 脚本，却接受这种自报家门的标识——read_webpage 被 403/429 时会换它重试。
POLITE_AGENT = (
    "langgraph-agent-brief/0.1 (research agent; +https://github.com/jrzzh/langgraph-agent-brief)"
)
HTTP_TIMEOUT = float(os.environ.get("LANGGRAPH_AGENT_HTTP_TIMEOUT", "15"))
MAX_TEXT_CHARS = 8000

#: 文件工具只允许这些后缀（与案例层保持一致，避免两套规则）
TEXT_SUFFIXES = frozenset(
    {".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".csv", ".log",
     ".py", ".js", ".ts", ".html", ".htm", ".xml", ".toml", ".ini", ".rst"}
)
#: 允许写入的后缀。**沙箱靠路径（workspace）而不是后缀**，所以这里放宽到常见
#: 源码与配置文件——早期只放开少数几种，结果模型写 index.html 被拒，
#: 只好改成 .txt 暂存再写脚本改名，白白多花一倍步骤。
#: 空后缀表示允许 ``Makefile`` / ``Dockerfile`` 这类无扩展名文件。
WRITE_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".json", ".jsonc", ".csv", ".tsv", ".log",
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss", ".less", ".svg", ".xml",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".m", ".mm", ".rs", ".go",
    ".java", ".kt", ".cs", ".php", ".rb", ".sh", ".bat", ".cmd", ".ps1",
    ".toml", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".env", ".cmake", ".mk",
    ".sql", ".gradle", ".gitignore", ".editorconfig", "",
})


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
#: 中文检索优先用搜狗。实测 Bing 对中文多关键词查询会返回"杭州旅游攻略"
#: 这类完全无关的结果（连英文长查询也会退化成杭州本地卡），
#: 而搜狗的结果页结构稳定、链接多为直达地址。
SOGOU_SEARCH_URL = "https://www.sogou.com/web"
BING_SEARCH_URL = "https://www.bing.com/search"

_SEARCH_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}


def _bigrams(text: str) -> set[str]:
    """取中文二元组，用来衡量结果与查询的相关度。"""
    grams: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for i in range(len(run) - 1):
            grams.add(run[i:i + 2])
    return grams


def _relevance(query: str, text: str) -> int:
    """查询与结果的贴合度，用来丢掉明显无关的结果。

    查询里有空格时按"关键词命中数"算——"杭州 上海 长途大巴 票价"命中 4 个
    才算强相关，而"西安咸阳国际机场"命中 0 个直接被扔掉；没有空格的中文
    长句退回二元组交集，英文按词命中。
    """
    words = [w for w in re.split(r"[\s,，、/|+]+", query) if w]
    low = text.lower()
    if len(words) >= 2:
        hits = sum(1 for w in words if w.lower() in low)
        return hits if hits < len(words) else hits + 1     # 全部命中再加一分
    want = _bigrams(query)
    if want:
        return len(want & _bigrams(text))
    return sum(1 for word in re.findall(r"[A-Za-z]{3,}", query) if word.lower() in low)


def _resolve_redirect(href: str) -> str:
    """搜狗/百度的结果链接多是跳转地址，尽量换成真实 URL（失败就原样返回）。"""
    if not href or not any(token in href for token in ("sogou.com/link", "baidu.com/link")):
        return href
    try:
        resp = requests.get(href, headers=_SEARCH_HEADERS,
                            timeout=min(HTTP_TIMEOUT, 8), allow_redirects=True)
        return resp.url or href
    except Exception:                              # noqa: BLE001 - 解析失败不影响可用性
        return href


def _search_sogou(query: str, limit: int) -> list[dict[str, str]]:
    resp = requests.get(SOGOU_SEARCH_URL, params={"query": query},
                        headers=_SEARCH_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    doc = html.fromstring(_strip_xml_decl(resp.text))

    out: list[dict[str, str]] = []
    for heading in doc.xpath("//h3"):
        link = heading.xpath(".//a")
        if not link:
            continue
        href = link[0].get("href") or ""
        if not href.startswith("http"):
            continue
        title = _squash(link[0].text_content())
        parent = heading.getparent()
        snippet = _squash(" ".join(parent.xpath(".//text()"))) if parent is not None else ""
        snippet = snippet.replace(title, "").strip()[:300]
        out.append({"title": title, "url": _resolve_redirect(href), "snippet": snippet})
        if len(out) >= limit:
            break
    return out


def _search_wikipedia(query: str, limit: int) -> list[dict[str, str]]:
    """维基百科搜索 API：不受反爬影响，返回的链接可以直接用 read_webpage 打开。

    对"线路/车站/里程/历史票价"这类背景事实特别有用——搜索引擎被拦时，
    这是少数还能拿到**可引用且可核验**内容的通道。
    """
    resp = requests.get(
        "https://zh.wikipedia.org/w/api.php",
        params={
            "action": "query", "list": "search", "srsearch": query,
            "format": "json", "srlimit": max(1, int(limit)), "utf8": 1,
        },
        headers={"User-Agent": POLITE_AGENT},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    hits = (resp.json().get("query") or {}).get("search") or []
    out: list[dict[str, str]] = []
    for hit in hits:
        title = str(hit.get("title") or "").strip()
        if not title:
            continue
        snippet = re.sub(r"<[^>]+>", "", str(hit.get("snippet") or ""))
        out.append({
            "title": f"{title}（维基百科）",
            "url": "https://zh.wikipedia.org/wiki/" + requests.utils.quote(title),
            "snippet": _squash(snippet)[:300],
        })
    return out


def _search_bing(query: str, limit: int) -> list[dict[str, str]]:
    resp = requests.get(BING_SEARCH_URL, params={"q": query, "setlang": "zh-Hans"},
                        headers=_SEARCH_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    doc = html.fromstring(_strip_xml_decl(resp.text))

    out: list[dict[str, str]] = []
    for item in doc.xpath("//li[contains(@class,'b_algo')]"):
        link = item.xpath(".//h2/a")
        if not link:
            continue
        out.append({
            "title": _squash(link[0].text_content()),
            "url": link[0].get("href", ""),
            "snippet": _squash(" ".join(p.text_content() for p in item.xpath(".//p")))[:300],
        })
        if len(out) >= limit:
            break
    return out


@tool(
    "search_web",
    description="搜索公开网页并返回候选来源（标题、链接、摘要）。"
    "查询用 2~4 个关键词，不要写成整句；可以加 site:域名 限定站点。",
)
def search_web(query: str, limit: int = 5) -> str:
    """三路检索（搜狗 / 维基百科 / Bing），按贴合度合并去重后返回 JSON 数组。

    为什么合并而不是"第一个成功就用"：实测搜狗会返回"DeepSeek 官网"这类
    非空但完全无关的结果。一旦把第一条通道当权威，后面更可靠的通道就永远
    没机会出场。合并 + 按相关度排序，模型看到的是"最像的几条"。
    """
    wanted = max(1, int(limit))
    problems: list[str] = []
    scored: list[tuple[int, int, dict[str, str]]] = []
    seen: set[str] = set()

    for rank, engine in enumerate((_search_sogou, _search_wikipedia, _search_bing)):
        try:
            items = engine(query, wanted)
        except Exception as exc:                   # noqa: BLE001 - 换下一个引擎
            problems.append(f"{engine.__name__}={type(exc).__name__}")
            continue
        if not items:
            problems.append(f"{engine.__name__}=空")
        for item in items:
            url = item.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            text = f"{item.get('title', '')} {item.get('snippet', '')}"
            scored.append((_relevance(query, text), rank, item))

    if not scored:
        return f"[检索失败: {'; '.join(problems)}]"

    # 相关度优先，同分时保持通道权重（搜狗 > 维基 > Bing）
    scored.sort(key=lambda row: (-row[0], row[1]))
    strong = [item for score, _rank, item in scored if score >= 2]
    weak = [item for score, _rank, item in scored if score == 1]
    picked = (strong + weak)[:wanted]
    if not picked:                                 # 全都不贴合：退回原始顺序
        picked = [item for _score, _rank, item in scored[:wanted]]
    return json.dumps(picked, ensure_ascii=False)


@tool("read_webpage", description="读取公开网页正文，返回纯文本。")
def read_webpage(url: str) -> str:
    """取正文文本；脚本、样式、导航一律丢掉。

    先伪装成浏览器 UA；被 403/429 拦下时换用"会报家门的"UA 再试一次——
    维基百科这类站点专门拒绝伪装成浏览器的脚本，却接受合规的机器人标识。
    """
    doc = None
    failure = ""
    for headers in (
        {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
        {"User-Agent": POLITE_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
    ):
        try:
            resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            doc = html.fromstring(_strip_xml_decl(resp.text))
            break
        except Exception as exc:                   # noqa: BLE001 - 换个 UA 再试
            failure = f"{type(exc).__name__}: {exc}"
    if doc is None:
        return f"[读取失败: {failure}]"

    for bad in doc.xpath("//script|//style|//nav|//footer|//header|//aside"):
        bad.getparent().remove(bad)

    # 维基百科正文在 #mw-content-text 里，直接取它，避免整页导航噪声
    if "wikipedia.org" in url:
        body = doc.xpath("//div[@id='mw-content-text']")
        if body:
            doc = body[0]

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
    shell_policy=None,
) -> list[Any]:
    """按名字挑出工具对象。

    ``names`` 为 ``None`` 表示全给；给了 workspace / read_paths 才有文件工具。
    未知名字直接忽略（UI 可能残留旧开关），不会因此崩掉整轮。

    ``shell_policy`` 是 shell 工具的审批回调：给了它，需要确认的命令会先问用户；
    不给则白名单是唯一防线（命令行场景）。
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

    # 浏览器：只有本机存在 Chrome/Edge 时才注入，否则该工具只会返回错误
    wanted_browser = {"browser_dom", "browser_screenshot"}
    if root is not None and (wanted is None or wanted & wanted_browser):
        from .browser import browser_available, make_browser_tools

        if browser_available():
            for item in make_browser_tools(workspace=root):
                if wanted is None or item.name in wanted:
                    picked.append(item)

    # shell 单独处理：没有 workspace 就不给（cwd 必须锁在沙箱内）
    if (wanted is None or "shell" in wanted) and root is not None:
        from .shell import make_shell_tool

        picked.append(make_shell_tool(policy=shell_policy, workspace=root))
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