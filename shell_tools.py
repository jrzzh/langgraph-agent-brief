# -*- coding: utf-8 -*-
"""workspace 内的只读 shell 命令工具。

**这是本项目里风险最高的一个工具**，因此设计上比 file_tools 严格得多：

1. **白名单优先于黑名单**：只放行若干只读命令（列目录、查版本、看 git 状态），
   其余一律拒绝。黑名单永远会漏，白名单只会在需要时被显式放宽。
2. **命令名与参数名对齐外部审批契约**：工具名为 ``shell``、参数名为 ``command``，
   这样接入 agent-ui 这类前端时，其 ``assess()`` 策略能正确识别并触发审批弹窗；
   换个参数名（例如 ``argv``）会让审批**静默失效**。
3. **不使用 shell 解释器**：``shell=False`` + ``shlex.split``，因此 ``& | ; > <``
   之类元字符不会被解释；即便如此仍在入口显式拒绝，作为第二道防线。
4. **密钥不出进程**：子进程环境变量按白名单重建，``DEEPSEEK_API_KEY`` /
   ``DASHSCOPE_API_KEY`` 等一律不传入——否则模型一条 ``echo %DEEPSEEK_API_KEY%``
   就能把密钥打出来。
5. **工作目录锁死**：``cwd`` 固定为 workspace 根目录；参数里出现绝对路径、盘符或
   ``..`` 直接拒绝，防止绕过文件工具的沙箱。
6. **限时限额**：默认 20 秒超时；stdout / stderr 各自截断，结果里带 ``truncated``。
"""

from __future__ import annotations

import json
import locale
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from langchain_core.tools import tool

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = Path(BASE_DIR) / "workspace"

WORKSPACE_ROOT: Path = DEFAULT_ROOT

DEFAULT_TIMEOUT = 20
MAX_TIMEOUT = 60
MAX_STDOUT_CHARS = 100_000
MAX_STDERR_CHARS = 20_000

#: 允许执行的命令白名单。
#: 值为 None 表示该命令的后续参数不限定；为元组时，第二个词必须在其中。
#:
#: 取舍依据（都是实测结论）：
#: - **只列真实可执行文件**。``dir`` / ``echo`` 是 cmd 内建命令，``shell=False``
#:   下根本不存在同名 exe，必然 COMMAND_NOT_FOUND——列进来只会误导。列目录请用
#:   ``file_tools.list_workspace_files()``。
#: - **git 只放不过作者信息的子命令**。``git log`` / ``git show`` 会打印提交者
#:   姓名与邮箱，等于把个人信息漏进模型上下文，故排除。
ALLOWED_COMMANDS: dict[str, tuple[str, ...] | None] = {
    # 查找可执行文件位置
    "where": None,
    "which": None,
    # 版本查询（只放只读子命令，绝不放开 -c / -m）
    "python": ("--version", "-V"),
    "pip": ("list", "--version", "show", "freeze"),
    # git 只读且不含作者信息的子命令
    "git": ("status", "branch", "diff", "--version"),
}

#: shell 元字符：即便不经过 shell 解释器，也在入口拒绝，避免语义歧义
_SHELL_META = re.compile(r"[&|;<>^`\r\n]")

#: 禁止出现在参数里的绝对路径形态（Windows 盘符 / UNC / POSIX 根）
_ABS_PATH = re.compile(r"^(?:[A-Za-z]:|\\\\|//|/[^/])")

#: 子进程可见的环境变量白名单（其余全部剔除）
_ENV_KEEP = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8",
}


class ShellToolError(Exception):
    """带错误码的 shell 工具异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def set_workspace_root(path: str | os.PathLike | None) -> Path:
    """设置工作目录；传 None 恢复默认的 ``workspace/``。"""
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = Path(path).expanduser().resolve() if path else DEFAULT_ROOT
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_ROOT


def workspace_root() -> Path:
    return WORKSPACE_ROOT


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _split(command: str) -> list[str]:
    """把命令字符串拆成 argv；不做任何 shell 展开。"""
    if not isinstance(command, str) or not command.strip():
        raise ShellToolError("INVALID_ARGUMENT", "command 必须是非空字符串")
    if len(command) > 500:
        raise ShellToolError("INVALID_ARGUMENT", "command 不能超过 500 字符")
    if _SHELL_META.search(command):
        raise ShellToolError("SHELL_METACHAR", "命令不能包含 & | ; < > ^ ` 或换行")
    try:
        # Windows 下用非 POSIX 模式，反斜杠不会被当转义符
        argv = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        raise ShellToolError("INVALID_ARGUMENT", f"命令无法解析: {exc}") from exc
    if not argv:
        raise ShellToolError("INVALID_ARGUMENT", "命令解析后为空")
    return argv


def _check_allowed(argv: list[str]) -> None:
    """命令必须命中白名单。"""
    cmd = argv[0].lower()
    if cmd not in ALLOWED_COMMANDS:
        raise ShellToolError(
            "COMMAND_NOT_ALLOWED",
            f"命令 {argv[0]!r} 不在只读白名单内；允许: "
            + ", ".join(sorted(ALLOWED_COMMANDS)),
        )
    allowed_sub = ALLOWED_COMMANDS[cmd]
    if allowed_sub is not None:
        sub = argv[1].lower() if len(argv) > 1 else ""
        if sub not in {s.lower() for s in allowed_sub}:
            raise ShellToolError(
                "SUBCOMMAND_NOT_ALLOWED",
                f"{argv[0]} 只允许子命令: {', '.join(allowed_sub)}",
            )


def _check_args(argv: list[str]) -> None:
    """参数里不允许绝对路径或上跳目录。"""
    for arg in argv[1:]:
        if _ABS_PATH.match(arg):
            raise ShellToolError("PATH_NOT_ALLOWED", f"参数不能是绝对路径: {arg}")
        parts = arg.replace("\\", "/").split("/")
        if ".." in parts:
            raise ShellToolError("PATH_NOT_ALLOWED", f"参数不能包含 ..: {arg}")


def _safe_env() -> dict[str, str]:
    """子进程环境变量：只保留白名单键，密钥类一律剔除。"""
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _decode(raw: bytes) -> str:
    """按本机首选编码解码，失败回退 UTF-8 并替换非法字节。"""
    if not raw:
        return ""
    for enc in (locale.getpreferredencoding(False) or "utf-8", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 审批分级
# ---------------------------------------------------------------------------
#: 完全不需要打扰用户的命令：纯查询、无副作用、不泄露环境
SAFE_COMMANDS = frozenset({"where", "which"})
#: 子命令级的"安全"：版本查询对任何白名单命令都无副作用
SAFE_SUBCOMMANDS = frozenset({"--version", "-v"})


def needs_approval(command: str) -> bool:
    """这条命令是否需要用户确认。

    分级原则：**只读且无信息价值**的直接放行，其余一律问一次。

    - 放行：``where`` / ``which`` / 任何 ``--version`` 查询——纯粹查位置和版本；
    - 询问：``git status/branch/diff``、``pip list/show/freeze``——会暴露仓库结构、
      已装包清单等，对用户来说有信息价值，值得确认一下。

    白名单外的命令**不走审批**，因为 :func:`_check_allowed` 会先直接拒绝——
    给模型"申请执行任意命令"的口子，等于把白名单变成可谈判的。
    """
    try:
        argv = _split(command)
    except ShellToolError:
        return True                     # 拆不出来就按"需要确认"处理
    if not argv:
        return True
    cmd = argv[0].lower()
    if cmd in SAFE_COMMANDS:
        return False
    sub = argv[1].lower() if len(argv) > 1 else ""
    return sub not in {s.lower() for s in SAFE_SUBCOMMANDS}


# ---------------------------------------------------------------------------
# 实现层
# ---------------------------------------------------------------------------
def shell_impl(command: str, timeout: int = DEFAULT_TIMEOUT,
               policy: "Callable[[str], bool] | None" = None) -> dict:
    """在 workspace 内执行一条白名单内的只读命令。

    ``policy`` 是外部 UI 提供的审批回调（``agent-ui`` 的 ``make_policy(ask_user)``）：
    非空时，"需要确认"的命令会先问一句，用户拒绝则返回 ``APPROVAL_DENIED``。
    ``None`` 表示没有审批通道（本项目命令行）——此时白名单就是唯一防线。
    """
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT:
        raise ShellToolError("INVALID_ARGUMENT", f"timeout 必须为 1~{MAX_TIMEOUT} 的整数")

    argv = _split(command)
    _check_allowed(argv)
    _check_args(argv)

    # 审批放在全部静态校验之后：非法命令直接拒，不该拿它去打扰用户
    if policy is not None and needs_approval(command):
        if not policy(f"是否允许执行以下只读命令？\n\n{command}"):
            return {
                "ok": False,
                "code": "APPROVAL_DENIED",
                "argv": argv,
                "cwd": WORKSPACE_ROOT.name,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "truncated": False,
                "duration_s": 0.0,
                "message": "用户未允许执行该命令；如需继续请换一种只读查询方式",
            }

    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    cwd = WORKSPACE_ROOT
    t0 = time.time()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env=_safe_env(),
            capture_output=True,
            timeout=timeout,
            shell=False,                 # 绝不经过 shell 解释器
        )
    except subprocess.TimeoutExpired as exc:
        out = _decode(exc.stdout or b"")[:MAX_STDOUT_CHARS]
        err = _decode(exc.stderr or b"")[:MAX_STDERR_CHARS]
        return {
            "ok": False,
            "code": "TIMEOUT",
            "argv": argv,
            "cwd": cwd.name,
            "returncode": None,
            "stdout": out,
            "stderr": err,
            "truncated": False,
            "duration_s": round(time.time() - t0, 2),
            "message": f"命令超过 {timeout}s 被终止",
        }
    except FileNotFoundError as exc:
        raise ShellToolError("COMMAND_NOT_FOUND", f"找不到命令: {argv[0]}") from exc
    except OSError as exc:
        raise ShellToolError("EXEC_ERROR", f"执行失败: {type(exc).__name__}") from exc

    stdout = _decode(proc.stdout)
    stderr = _decode(proc.stderr)
    trunc_out = len(stdout) > MAX_STDOUT_CHARS
    trunc_err = len(stderr) > MAX_STDERR_CHARS
    return {
        "ok": proc.returncode == 0,
        "argv": argv,
        "cwd": cwd.name,
        "returncode": proc.returncode,
        "stdout": stdout[:MAX_STDOUT_CHARS],
        "stderr": stderr[:MAX_STDERR_CHARS],
        "truncated": bool(trunc_out or trunc_err),
        "duration_s": round(time.time() - t0, 2),
    }


def _to_json(fn, *args, **kwargs) -> str:
    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except ShellToolError as exc:
        return json.dumps({"ok": False, "code": exc.code, "message": exc.message},
                          ensure_ascii=False)
    except Exception as exc:                      # noqa: BLE001 - 兜底不外抛
        return json.dumps({"ok": False, "code": "INTERNAL_ERROR",
                           "message": f"{type(exc).__name__}: {exc}"},
                          ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
SHELL_DESCRIPTION = """在 workspace 内执行一条**只读**命令，返回退出码、stdout 与 stderr（JSON）。

白名单只放真实可执行文件（cmd 内建命令如 ``dir`` / ``echo`` 不在其中）：
``where`` / ``which`` / ``python --version|-V`` / ``pip list|show|freeze|--version`` /
``git status|branch|diff|--version``。完整清单见模块常量 ``ALLOWED_COMMANDS``。

不允许管道与重定向、绝对路径、``..``；子进程不继承任何密钥；默认 20 秒超时。
参数名固定为 ``command``，以便外部前端的安全策略能够识别并触发审批。
非版本查询类的命令在执行前会请求用户确认，被拒绝时返回 ``APPROVAL_DENIED``。"""


def make_shell_tool(policy=None):
    """构造一个 ``shell`` 工具，可绑定审批回调。

    ``policy`` 签名 ``(question: str) -> bool``，通常传
    ``agent_ui.core.ask.make_policy(ask_user)``：用户拒绝或超时都返回 ``False``
    （fail-closed），工具会返回 ``APPROVAL_DENIED`` 而不是执行命令。
    ``policy=None`` 表示没有审批通道（本项目命令行），此时白名单是唯一防线。

    工具名固定 ``shell``、参数名固定 ``command``——外部 UI 的
    ``approval_policy.assess()`` 靠这两样识别 shell 类调用，改名会让审批静默失效。
    """

    def _run(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        return _to_json(shell_impl, command, timeout, policy)

    return tool("shell", description=SHELL_DESCRIPTION)(_run)


#: 无审批通道的版本（本项目命令行 / Streamlit 用）
shell = make_shell_tool(None)

SHELL_TOOLS = [shell]


def allowed_commands_text() -> str:
    """人读的白名单描述，供界面与文档展示。"""
    parts = []
    for cmd, subs in sorted(ALLOWED_COMMANDS.items()):
        parts.append(cmd if subs is None else f"{cmd} {'/'.join(subs)}")
    return "；".join(parts)