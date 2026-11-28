"""本地命令执行后端：让 deepagents 的 ``execute`` 工具真正可用。

deepagents 自带一个 ``execute`` 工具，但只有当 backend 实现
``SandboxBackendProtocol`` 时才会真正执行，否则每次调用都返回
"Execution not available"。纯 ``FilesystemBackend`` 不满足这个条件，
所以模型会看到一个"永远失败"的工具——既浪费轮次，也拉低工具成功率。

本模块在 ``FilesystemBackend`` 的基础上补上执行能力：

- 文件操作完全沿用父类（路径沙箱、虚拟路径语义都不变）；
- 新增 ``execute`` / ``aexecute``：``cwd`` 固定为 workspace 根，命令走 shell 解释器；
- **环境变量剔除密钥**：名字里带 KEY / TOKEN / SECRET / PASSWORD 的一律不传给子进程；
- **破坏性命令黑名单**：``rm -rf``、``del /f/s``、``format``、``shutdown`` 等直接拒绝；
- 超时与输出截断：默认 120 秒、输出 30k 字符，超时返回 ``exit_code=None``。

设计取舍：黑名单只能挡住明显的误操作，挡不住蓄意的破坏——真正隔离要靠容器/虚拟机。
这里的定位是"本机开发用的可信执行"，不是安全边界。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

#: 默认超时（秒）。编译类任务比查询慢，给足余量。
DEFAULT_EXEC_TIMEOUT = 120
#: 超时上限，防止模型写个离谱的数字把一轮拖死。
MAX_EXEC_TIMEOUT = 600
MAX_OUTPUT_CHARS = 30_000

#: 明显破坏性的命令模式。命中即拒绝，不走审批——这类命令没有"值得问一次"的余地。
_DENY_PATTERNS = (
    r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+",
    r"\bdel\s+/[fsq]",
    r"\brd\s+/s",
    r"\brmdir\s+/s",
    r"\bformat\s+[a-zA-Z]:",
    r"\bmkfs\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\breg\s+delete\b",
    r"\bRemove-Item\b.*-Recurse",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
)
_DENY_RE = re.compile("|".join(_DENY_PATTERNS), re.IGNORECASE)

#: 名字里出现这些片段的环境变量不传子进程（密钥别名覆盖常见写法）
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def _kill_tree(pid: int) -> None:
    """连子孙一起杀。

    Windows 上 ``start /B`` 起的服务是 cmd.exe 的子进程，只杀父进程会留下孤儿；
    ``taskkill /T`` 按进程树清理。POSIX 上对进程组发 SIGKILL。
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:                              # noqa: BLE001 - 清理失败不该影响返回值
        pass


class CommandDenied(RuntimeError):
    """命令被黑名单拒绝。"""

    def __init__(self, command: str) -> None:
        super().__init__(f"命令被安全策略拒绝（疑似破坏性操作）：{command}")
        self.command = command


def scrub_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """复制环境变量，去掉名字像密钥的那些。"""
    source = os.environ if env is None else env
    out: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if any(marker in upper for marker in _SECRET_MARKERS):
            continue
        out[key] = value
    out.setdefault("PYTHONIOENCODING", "utf-8")
    out.setdefault("PYTHONUTF8", "1")
    return out


def check_command(command: str) -> None:
    """黑名单校验；命中抛 :class:`CommandDenied`。"""
    if not command or not command.strip():
        raise CommandDenied(command)
    match = _DENY_RE.search(command)
    if match:
        raise CommandDenied(command)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "cp1252"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


class LocalShellBackend(FilesystemBackend):
    """带本地命令执行能力的文件后端。

    通过 ``SandboxBackendProtocol.register`` 注册成虚拟子类——deepagents 用
    ``isinstance(backend, SandboxBackendProtocol)`` 判断能否执行，而该协议是
    抽象基类（无抽象方法），注册比多继承更干净。
    """

    #: 默认 120 秒；``execute`` 的 timeout 参数可以覆盖
    default_timeout: int = DEFAULT_EXEC_TIMEOUT

    def __init__(
        self,
        *,
        root_dir: str | os.PathLike,
        virtual_mode: bool = True,
        policy: Callable[[str], bool] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(root_dir=str(root_dir), virtual_mode=virtual_mode)
        self._root = Path(root_dir).expanduser().resolve()
        self._policy = policy
        self._timeout = max(1, min(int(timeout or self.default_timeout), MAX_EXEC_TIMEOUT))
        self._env = scrub_env(env)
        self._cancel_check = cancel_check

    # -- 协议要求 ---------------------------------------------------------
    @property
    def id(self) -> str:
        return "local-shell"

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """在工作目录里执行一条命令。"""
        try:
            check_command(command)
        except CommandDenied as exc:
            return ExecuteResponse(output=str(exc), exit_code=None, truncated=False)

        if self._policy is not None and not self._policy(
            f"是否允许在项目目录内执行以下命令？\n\n{command}"
        ):
            return ExecuteResponse(
                output="用户未允许执行该命令（APPROVAL_DENIED）。如需继续，请换一种不需要执行命令的方式。",
                exit_code=None,
                truncated=False,
            )

        limit = max(1, min(int(timeout or self._timeout), MAX_EXEC_TIMEOUT))
        self._root.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()

        # 用临时文件而不是管道接输出：模型可能用 ``start /B`` 起一个常驻服务，
        # 它会继承父进程的 stdout 句柄。用管道时 ``subprocess.run(timeout=)``
        # 超时后只杀掉 cmd.exe，随后 ``communicate()`` 会等管道关闭——
        # 而服务还活着，于是**永久挂起**（实测卡了 30 分钟）。
        # 文件句柄没有这个问题：父进程退出即可读，服务继续跑也不影响。
        with tempfile.TemporaryFile() as out_fh, tempfile.TemporaryFile() as err_fh:
            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(self._root),
                    env=self._env,
                    shell=True,             # 允许管道与串行命令；安全性由黑名单兜底
                    stdout=out_fh,
                    stderr=err_fh,
                )
            except OSError as exc:
                return ExecuteResponse(output=f"[执行失败] {type(exc).__name__}: {exc}",
                                       exit_code=None, truncated=False)

            deadline = time.monotonic() + limit
            exit_code: int | None = None
            note = ""
            while True:
                try:
                    exit_code = proc.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if self._cancel_check is not None and self._cancel_check():
                        _kill_tree(proc.pid)
                        note = "\n[已取消] 用户中断，命令被终止"
                        break
                    if time.monotonic() >= deadline:
                        _kill_tree(proc.pid)
                        note = f"\n[超时] 命令超过 {limit}s 被终止"
                        break

            out_fh.seek(0)
            err_fh.seek(0)
            text = (_decode(out_fh.read()) + _decode(err_fh.read())).strip()

        elapsed = time.perf_counter() - started
        if not text:
            text = f"[无输出] 退出码 {exit_code}，耗时 {elapsed:.1f}s"
        text = text + note
        return ExecuteResponse(
            output=text[:MAX_OUTPUT_CHARS],
            exit_code=None if note else exit_code,
            truncated=len(text) > MAX_OUTPUT_CHARS,
        )

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """异步版本：丢到线程里跑，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.execute, command, timeout=timeout)


#: 注册为虚拟子类，让 ``supports_execution()`` 返回 True
SandboxBackendProtocol.register(LocalShellBackend)


__all__ = [
    "CommandDenied",
    "DEFAULT_EXEC_TIMEOUT",
    "LocalShellBackend",
    "MAX_EXEC_TIMEOUT",
    "MAX_OUTPUT_CHARS",
    "check_command",
    "scrub_env",
]
