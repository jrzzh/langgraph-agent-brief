"""浏览器工具：用本机 Chrome/Edge 的 headless 模式做页面验证。

为什么不用 Playwright：它要额外装包 + 下载 Chromium（约 150 MB），而这台机器上
Chrome 和 Edge 都已经存在，headless 模式本身就能截图和导出 DOM。够用的前提下，
不引入新依赖更划算。

三个能力：

- ``browser_dom``：等 JS 执行完，导出最终 DOM（可判断元素是否存在、文案是否正确），
  同时把控制台报错抓出来——这是"页面白屏/报错"最直接的证据；
- ``browser_screenshot``：截图存进工作目录，作为可视化证据；
- ``browser_available``：探测本机有没有可用的浏览器。

**局限**：拿不到像素级判断（模型看不了图），所以"画面精美"这类要求无法自证，
只能证明"页面能加载、无 JS 报错、DOM 结构符合预期"。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800
DEFAULT_WAIT_MS = 1500
DEFAULT_TIMEOUT = 60
MAX_DOM_CHARS = 20_000
MAX_CONSOLE_CHARS = 4_000

#: 常见安装位置（Get-Command 不一定能查到 chrome.exe，因为它通常不在 PATH）
_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

#: 控制台错误里值得单独列出的关键字
_ERROR_MARKERS = ("ERROR", "Uncaught", "TypeError", "ReferenceError", "SyntaxError", "Failed to load")


def find_browser() -> str | None:
    """找一个可用的 Chromium 内核浏览器，找不到返回 ``None``。"""
    override = os.environ.get("LANGGRAPH_AGENT_BROWSER", "").strip()
    if override and Path(override).is_file():
        return override
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    for raw in _CANDIDATES:
        path = Path(os.path.expandvars(raw))
        if path.is_file():
            return str(path)
    return None


def browser_available() -> bool:
    return find_browser() is not None


def _resolve_target(url: str) -> str:
    """``path/to/index.html`` 这种相对路径转成 file:// URI；远程地址原样返回。"""
    if "://" in url:
        return url
    path = Path(url).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve().as_uri()


def _run(browser: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    profile = tempfile.mkdtemp(prefix="agent-browser-")
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        # 受限环境里 crashpad 起不来会让整个进程自杀，必须关掉崩溃上报
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--allow-file-access-from-files",
        f"--user-data-dir={profile}",
        *args,
    ]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def browser_dom(url: str, wait_ms: int = DEFAULT_WAIT_MS, timeout: int = DEFAULT_TIMEOUT) -> str:
    """导出渲染后的 DOM 与控制台报错（JSON）。"""
    browser = find_browser()
    if browser is None:
        return json.dumps({"ok": False, "error": "本机没有找到 Chrome 或 Edge"}, ensure_ascii=False)
    target = _resolve_target(url)
    started = time.perf_counter()
    try:
        proc = _run(browser, [
            f"--virtual-time-budget={int(wait_ms)}",
            "--dump-dom",
            target,
        ], timeout)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": f"页面加载超过 {timeout}s"}, ensure_ascii=False)

    dom = _decode(proc.stdout)
    stderr = _decode(proc.stderr)
    # 浏览器自身的基础设施噪声（crashpad、gpu 初始化）不是页面错误，先滤掉
    _noise = ("crashpad", "chrome_main.cc", "gpu", "font", "dbus", "sandbox")
    console = [
        line.strip() for line in stderr.splitlines()
        if not any(token in line.lower() for token in _noise)
        and ("CONSOLE" in line or any(marker in line for marker in _ERROR_MARKERS))
    ]
    return json.dumps({
        "ok": proc.returncode == 0,
        "url": target,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "dom_chars": len(dom),
        "dom": dom[:MAX_DOM_CHARS],
        "console_errors": console[:20],
        "console_text": "\n".join(console)[:MAX_CONSOLE_CHARS],
    }, ensure_ascii=False)


def browser_screenshot(
    url: str,
    path: str = "browser-shot.png",
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    wait_ms: int = DEFAULT_WAIT_MS,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """截图保存到工作目录，返回 JSON（含路径与大小）。"""
    browser = find_browser()
    if browser is None:
        return json.dumps({"ok": False, "error": "本机没有找到 Chrome 或 Edge"}, ensure_ascii=False)

    target_file = Path(path).expanduser()
    if not target_file.is_absolute():
        target_file = Path.cwd() / target_file
    target_file.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        proc = _run(browser, [
            f"--virtual-time-budget={int(wait_ms)}",
            f"--window-size={int(width)},{int(height)}",
            f"--screenshot={target_file}",
            _resolve_target(url),
        ], timeout)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": f"截图超过 {timeout}s"}, ensure_ascii=False)

    size = target_file.stat().st_size if target_file.exists() else 0
    return json.dumps({
        "ok": proc.returncode == 0 and size > 0,
        "path": str(target_file),
        "bytes": size,
        "elapsed_s": round(time.perf_counter() - started, 2),
        "stderr": _decode(proc.stderr)[-300:] if size == 0 else "",
    }, ensure_ascii=False)


BROWSER_TOOLS = None            # 占位：由 tools.build_tools 按需构造


def make_browser_tools() -> list[Any]:
    """构造浏览器工具对象（只在检测到浏览器时调用）。"""
    from langchain_core.tools import tool

    @tool("browser_dom", description="用本机 headless 浏览器打开页面，返回渲染后的 DOM 与控制台报错（JSON）。")
    def _dom(url: str, wait_ms: int = DEFAULT_WAIT_MS) -> str:
        return browser_dom(url, wait_ms)

    @tool("browser_screenshot", description="用本机 headless 浏览器截图并保存到工作目录，返回文件路径。")
    def _shot(url: str, path: str = "browser-shot.png",
              width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
              wait_ms: int = DEFAULT_WAIT_MS) -> str:
        return browser_screenshot(url, path, width, height, wait_ms)

    return [_dom, _shot]


__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_WAIT_MS",
    "DEFAULT_WIDTH",
    "browser_available",
    "browser_dom",
    "browser_screenshot",
    "find_browser",
    "make_browser_tools",
]