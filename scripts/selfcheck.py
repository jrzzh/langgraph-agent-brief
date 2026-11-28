"""自检：不调用模型也能验证的部分。

用法::

    uv run python scripts/selfcheck.py            # 离线检查（毫秒级，不花钱）
    uv run python scripts/selfcheck.py --net      # 额外跑一次真实检索与抓取
    uv run python scripts/selfcheck.py --all      # 再加上真实模型的一轮对话

设计意图：把"改了代码但没跑过"的窗口压到最小。每项失败都会打印原因，
并以非零码退出，方便放进提交前的手动清单或 CI。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
EXAMPLE = REPO / "example"

results: list[tuple[str, bool, str]] = []


def check(name: str) -> None:
    """记录一项检查的开始（配合 assert 风格的分支使用）。"""
    results.append((name, True, ""))


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def step(name: str):
    """装饰器：把一项检查包起来，异常即失败，不中断后续检查。"""

    def wrap(fn):
        def run():
            started = time.perf_counter()
            try:
                detail = fn() or ""
                record(name, True, f"{time.perf_counter() - started:.2f}s {detail}".strip())
            except AssertionError as exc:
                record(name, False, str(exc))
            except Exception as exc:                       # noqa: BLE001
                record(name, False, f"{type(exc).__name__}: {exc}")

        run.__name__ = fn.__name__
        return run

    return wrap


# ---------------------------------------------------------------------------
# 1. 内核：导入轻、目录纯数据
# ---------------------------------------------------------------------------
@step("内核导入是惰性的（不加载 deepagents）")
def check_lazy_import() -> str:
    code = (
        "import sys, langgraph_agent; "
        "print('deepagents' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO,
    )
    assert out.returncode == 0, out.stderr.strip()[-300:]
    assert out.stdout.strip() == "False", "import langgraph_agent 就把 deepagents 拉进来了"
    return ""


@step("工具目录 = 9 项且为纯数据")
def check_catalog() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent import tool_specs

    items = tool_specs.catalog()
    assert len(items) == 9, f"期望 9 项，实际 {len(items)}"
    names = [name for name, _, _ in items]
    for expected in ("search_web", "read_webpage", "calculator",
                     "read_file", "write_file", "list_directory", "shell",
                     "browser_dom", "browser_screenshot"):
        assert expected in names, f"目录里缺少 {expected}"
    # shell 必须默认关闭：它是唯一能触碰真实进程的工具
    default_on = {name: enabled for name, _, enabled in items}
    assert default_on["shell"] is False, "shell 默认应该是关的"
    return f"{names}"


@step("shell 三级分级（放行 / 审批 / 直接拒绝）")
def check_shell_tiers() -> str:
    sys.path.insert(0, str(SRC))
    import json

    from langgraph_agent import shell

    # ① 放行：查位置、查版本
    assert shell.needs_approval("where python") is False, "where 不该弹窗"
    assert shell.needs_approval("python --version") is False, "--version 不该弹窗"
    # ② 需审批：会暴露仓库结构 / 已装包清单
    assert shell.needs_approval("git status") is True, "git status 应该弹窗"
    assert shell.needs_approval("pip list") is True, "pip list 应该弹窗"

    ws = REPO / "workspace"

    def run(command: str, policy) -> dict:
        # 走工具真实路径：_to_json 把 ShellToolError 转成错误码
        return json.loads(shell._to_json(shell.shell_impl, command, 20, policy, root=ws))

    # ③ 白名单外：不走审批，直接拒（不能给模型"申请执行任意命令"的口子）
    asked: list[str] = []
    denied = run("rm -rf /", lambda q: asked.append(q) or True)
    assert denied["code"] == "COMMAND_NOT_ALLOWED", denied
    assert not asked, "白名单外的命令不该弹窗"

    # ④ 用户拒绝 → APPROVAL_DENIED，且命令没跑
    refused = run("git status", lambda q: False)
    assert refused["code"] == "APPROVAL_DENIED", refused

    # ⑤ 用户允许 → 正常执行
    allowed = run("git branch --version", lambda q: True)
    assert allowed.get("returncode") is not None, allowed
    return ""


@step("shell 子进程看不到密钥")
def check_shell_env() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent import shell

    leaked = [
        key for key in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY")
        if key.upper() in shell._safe_env()
    ]
    assert not leaked, f"子进程环境里出现了密钥：{leaked}"
    return ""


# ---------------------------------------------------------------------------
# 2. 工具注入：开关、未知名字、workspace 门控
# ---------------------------------------------------------------------------
@step("工具按开关裁剪（disabled 生效，未知名字被忽略）")
def check_select() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent import tool_specs
    from langgraph_agent.tools import build_tools

    all_names = set(tool_specs.TOOL_NAMES)
    kept = build_tools(all_names - {"search_web", "calculator"})
    names = {t.name for t in kept}
    assert "search_web" not in names, "关掉的 search_web 还在"
    assert "calculator" not in names, "关掉的 calculator 还在"
    assert "read_webpage" in names, "没关的 read_webpage 丢了"

    mixed = build_tools({"calculator", "不存在的工具"})
    assert [t.name for t in mixed] == ["calculator"], "未知名字应当被忽略"
    return ""


@step("文件工具按 workspace 门控（没给目录就不给）")
def check_workspace_gate() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent import tool_specs
    from langgraph_agent.tools import build_tools

    without = {t.name for t in build_tools(tool_specs.TOOL_NAMES)}
    assert "write_file" not in without, "没有 workspace 却给了 write_file"
    assert "read_file" not in without, "没有 workspace 也没有附件，却给了 read_file"

    with_ws = {t.name for t in build_tools(tool_specs.TOOL_NAMES, workspace=REPO / "workspace")}
    assert {"read_file", "write_file", "list_directory"} <= with_ws, "给了 workspace 但文件工具没出来"
    return ""


@step("文件工具沙箱：越界读写被拒")
def check_sandbox() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent import tool_specs
    from langgraph_agent.tools import build_tools

    ws = REPO / "workspace"
    bound = {t.name: t for t in build_tools(tool_specs.TOOL_NAMES, workspace=ws)}
    probe = ws / "briefs" / "_selfcheck.md"

    written = bound["write_file"].invoke({"path": "briefs/_selfcheck.md", "content": "ok"})
    assert written.startswith("[已写入"), written
    assert probe.is_file(), "文件没有真的落盘"
    assert bound["read_file"].invoke({"path": "briefs/_selfcheck.md"}) == "ok"

    escape_write = bound["write_file"].invoke({"path": "../_escape.md", "content": "x"})
    assert escape_write.startswith("[写入失败"), escape_write
    escape_read = bound["read_file"].invoke({"path": "../../.env"})
    assert escape_read.startswith("[读取失败"), escape_read

    probe.unlink(missing_ok=True)
    return ""


@step("可写后缀覆盖常见源码（网页游戏 / C++ 迁移的前提）")
def check_write_suffixes() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent import tool_specs
    from langgraph_agent.tools import build_tools

    ws = REPO / "workspace"
    tools = {t.name: t for t in build_tools(tool_specs.TOOL_NAMES, workspace=ws)}
    tried = ("_suffix/index.html", "_suffix/css/style.css", "_suffix/js/main.js",
             "_suffix/src/main.cpp", "_suffix/Makefile")
    for rel in tried:
        out = tools["write_file"].invoke({"path": rel, "content": "x"})
        assert out.startswith("[已写入"), (rel, out)
    # 可执行文件仍然拒绝：写 .exe 没有正当理由
    blocked = tools["write_file"].invoke({"path": "_suffix/a.exe", "content": "x"})
    assert blocked.startswith("[写入失败"), blocked

    import shutil
    shutil.rmtree(ws / "_suffix", ignore_errors=True)
    return f"{len(tried)} 种后缀"


@step("计算器沙箱：只算数，不执行代码")
def check_calculator() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent.tools import calculator

    assert calculator.invoke({"expression": "(3+4)*2**3"}) == "56"
    bad = calculator.invoke({"expression": "__import__('os').system('echo x')"})
    assert bad.startswith("错误"), bad
    return ""


# ---------------------------------------------------------------------------
# 3. 案例层：两个实现都能构造
# ---------------------------------------------------------------------------
@step("命令执行后端可用（execute 不再是死工具）")
def check_execute() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent.sandbox import LocalShellBackend
    from deepagents.middleware.filesystem import supports_execution

    backend = LocalShellBackend(root_dir=REPO / "workspace", virtual_mode=True)
    assert supports_execution(backend), "deepagents 仍认为这个后端不能执行命令"

    ok = backend.execute("echo probe-ok")
    assert ok.exit_code == 0 and "probe-ok" in ok.output, ok
    return f"echo → exit={ok.exit_code}"


@step("常驻进程不挂死（start /B + 超时 + 取消）")
def check_exec_no_hang() -> str:
    import threading
    import time

    sys.path.insert(0, str(SRC))
    from langgraph_agent.sandbox import LocalShellBackend

    backend = LocalShellBackend(root_dir=REPO / "workspace", virtual_mode=True)

    # ① 后台服务 + 立即返回：曾经因为管道被服务继承而永久挂起
    started = time.perf_counter()
    backend.execute("start /B python -m http.server 8231", timeout=15)
    elapsed = time.perf_counter() - started
    assert elapsed < 15, f"后台服务把调用挂住了 {elapsed:.1f}s"

    # ② 前台常驻命令：超时必须返回并清理
    started = time.perf_counter()
    out = backend.execute("python -m http.server 8232", timeout=2)
    elapsed = time.perf_counter() - started
    assert "[超时]" in out.output and elapsed < 12, (elapsed, out.output[-60:])

    # ③ 取消回调能在命令中途打断
    stop = {"on": False}
    cancellable = LocalShellBackend(
        root_dir=REPO / "workspace", virtual_mode=True, cancel_check=lambda: stop["on"]
    )
    threading.Timer(1.0, lambda: stop.update(on=True)).start()
    started = time.perf_counter()
    out = cancellable.execute("python -m http.server 8233", timeout=60)
    elapsed = time.perf_counter() - started
    assert "[已取消]" in out.output and elapsed < 20, (elapsed, out.output[-60:])
    return f"后台 {elapsed:.1f}s 内返回"


@step("命令执行的破坏性黑名单与密钥隔离")
def check_execute_guard() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent.sandbox import LocalShellBackend, scrub_env

    backend = LocalShellBackend(root_dir=REPO / "workspace", virtual_mode=True)
    for bad in ("rm -rf /", "del /f /s /q C:\\", "format C:"):
        result = backend.execute(bad)
        assert result.exit_code is None and "拒绝" in result.output, (bad, result)

    env = scrub_env({"PATH": "x", "DEEPSEEK_API_KEY": "sk-1", "MY_TOKEN": "t", "HOME": "h"})
    assert "DEEPSEEK_API_KEY" not in env and "MY_TOKEN" not in env, env
    assert env.get("PATH") == "x", "非密钥变量不该被删掉"
    return ""


@step("浏览器工具已注册（本机有 Chrome/Edge 时）")
def check_browser_tools() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent import browser
    from langgraph_agent.tools import build_tools

    if not browser.browser_available():
        return "跳过：本机没有 Chrome/Edge"
    names = {t.name for t in build_tools(workspace=REPO / "workspace")}
    for expected in ("browser_dom", "browser_screenshot"):
        assert expected in names, f"缺工具 {expected}"
    return browser.find_browser() or ""


@step("案例层可导入（agent_brief / agent_deepagents）")
def check_example_imports() -> str:
    sys.path.insert(0, str(EXAMPLE))
    import agent_brief as brief
    import agent_deepagents as deep

    assert callable(deep.build_agent), "example 的 build_agent 不可调用"
    tools = [t.name for t in brief.BASE_TOOLS]
    assert tools == ["web_search", "web_fetch", "calculator"], tools
    assert Path(brief.SKILLS_DIR).is_dir(), f"技能目录不存在：{brief.SKILLS_DIR}"
    return f"技能 {len(list(Path(brief.SKILLS_DIR).glob('*/SKILL.md')))} 个"


@step("MCP 源支持统计/日志注入（无反向依赖）")
def check_mcp_configure() -> str:
    sys.path.insert(0, str(EXAMPLE))
    import agent_brief as brief
    import mcps.qwen_mcp as qwen

    fresh = brief.fresh_stats()
    qwen.configure(stats=fresh, logger=brief.log)
    assert qwen._stats is fresh, "统计容器没有真的注入"
    assert "agent_brief" not in sys.modules or qwen._log is brief.log, "日志器没有注入"
    return ""


# ---------------------------------------------------------------------------
# 4. 可选：真实联网
# ---------------------------------------------------------------------------
@step("真实检索（Bing）")
def check_search() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent.tools import search_web

    started = time.perf_counter()
    raw = search_web.invoke({"query": "LangGraph 是什么", "limit": 3})
    assert not raw.startswith("[检索失败"), raw[:120]
    items = json.loads(raw)
    assert items, "返回空列表"
    return f"{time.perf_counter() - started:.1f}s / {len(items)} 条"


@step("真实抓取（网页正文）")
def check_fetch() -> str:
    sys.path.insert(0, str(SRC))
    from langgraph_agent.tools import read_webpage

    started = time.perf_counter()
    text = read_webpage.invoke({"url": "https://modelcontextprotocol.io/specification"})
    assert not text.startswith("[读取失败"), text[:120]
    assert len(text) > 200, f"正文太短：{len(text)}"
    return f"{time.perf_counter() - started:.1f}s / {len(text)} 字"


# ---------------------------------------------------------------------------
# 5. 可选：真实模型一轮
# ---------------------------------------------------------------------------
@step("真实模型一轮（注入工具 + 技能）")
def check_model_run() -> str:
    sys.path.insert(0, str(EXAMPLE))
    import agent_deepagents as deep

    started = time.perf_counter()
    report = deep.run_task(topic="用两句话说明 MCP 是什么", enable_files=False)
    body = (report.get("brief") or "").strip()
    assert body, "简报为空"
    return f"{time.perf_counter() - started:.1f}s / 正文 {len(body)} 字"


def main() -> int:
    parser = argparse.ArgumentParser(description="离线自检")
    parser.add_argument("--net", action="store_true", help="额外跑真实检索与抓取")
    parser.add_argument("--all", action="store_true", help="再加上真实模型的一轮对话")
    args = parser.parse_args()

    check_lazy_import()
    check_catalog()
    check_select()
    check_workspace_gate()
    check_sandbox()
    check_calculator()
    check_write_suffixes()
    check_shell_tiers()
    check_shell_env()
    check_execute()
    check_execute_guard()
    check_browser_tools()
    check_exec_no_hang()
    check_example_imports()
    check_mcp_configure()
    if args.net or args.all:
        check_search()
        check_fetch()
    if args.all:
        check_model_run()

    width = max(len(name) for name, _, _ in results)
    print()
    for name, ok, detail in results:
        mark = "✅" if ok else "❌"
        print(f"  {mark} {name.ljust(width)}  {detail}")
    failed = [name for name, ok, _ in results if not ok]
    print()
    if failed:
        print(f"  {len(failed)} 项失败：{'、'.join(failed)}")
        return 1
    print(f"  全部 {len(results)} 项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
