# -*- coding: utf-8 -*-
"""workspace 文件读写工具。

设计要点（借鉴成熟的 agent 文件工具做法）：
- **沙箱**：所有路径限定在项目内 ``workspace/``，禁止绝对路径、``..``、符号链接，
  解析后必须仍在根目录内。
- **白名单后缀**：只放开文本类扩展名，二进制一律拒绝。
- **限额**：读取 ≤ 1 MB、写入 ≤ 20 万字符、单次返回 ≤ 30000 字符。
- **写入默认不覆盖**：必须显式 ``overwrite=True``。
- **原子写**：先写同目录临时文件再 ``os.replace``，避免中途失败留下半截文件。
- **不抛异常**：出错返回 ``{"ok": false, "code": ..., "message": ...}`` 的 JSON 字符串，
  便于模型据此自我纠正，也不会中断图执行。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from langchain_core.tools import tool

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = Path(BASE_DIR) / "workspace"

#: 当前生效的 workspace 根目录（可由 set_workspace_root 覆盖）
WORKSPACE_ROOT: Path = DEFAULT_ROOT

MAX_READ_BYTES = 1_000_000        # 10 MB 太大，按 1 MB 限制
MAX_WRITE_CHARS = 200_000
MAX_CHARS_RANGE = (100, 30000)

ALLOWED_SUFFIXES = {
    ".txt", ".md", ".json", ".csv", ".log",
    ".yaml", ".yml", ".toml", ".py", ".html", ".xml",
}

# Windows 保留设备名（不允许作为文件名主干）
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_BAD_CHARS = set('<>:"|?*')


class FileToolError(Exception):
    """带错误码的文件工具异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def set_workspace_root(path: str | os.PathLike | None) -> Path:
    """设置本次运行的文件根目录；传 None 恢复默认的 ``workspace/``。

    仅做赋值，**不做安全检查**——对外入口请用 :func:`use_workspace`。
    """
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = Path(path).expanduser().resolve() if path else DEFAULT_ROOT
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_ROOT


def validate_workspace(raw: str | os.PathLike, *, strict: bool = True) -> Path:
    """校验用户指定的 workspace 是否可以作为文件工具根目录。

    默认的 ``<项目>/workspace`` 是项目内子目录，沙箱天然收敛；一旦允许外部目录，
    就必须挡住那些"一旦指错就等于把机器交出去"的位置。

    两种模式：

    - ``strict=True``（默认，本项目命令行/自带界面）：额外拒绝用户主目录、项目根
      及其**祖先**、含 ``.git`` 的目录——这些规则是为了"别让模型改写本项目源码"。
    - ``strict=False``（被外部 UI 嵌入）：目录作用域由**宿主**决定（用户在界面上
      明确选了一个工作目录），我们只挡住"显然不该写"的位置，即盘符根/文件系统根。
      否则用户在界面选 ``D:\\某目录``（很可能是本项目的上级）会被我们拒绝并
      **静默回退**到项目内 ``workspace/``，模型读写的地方就和用户以为的不一致了。

    两种模式共同要求：必须是**已存在的目录**。返回解析后的绝对路径；
    不合法时抛 :class:`FileToolError`。
    """
    try:
        p = Path(raw).expanduser()
    except (TypeError, ValueError) as exc:
        raise FileToolError("INVALID_ARGUMENT", f"workspace 路径无法解析: {raw!r}") from exc

    if not p.exists():
        raise FileToolError("WORKSPACE_NOT_FOUND", f"workspace 目录不存在: {p}")
    if not p.is_dir():
        raise FileToolError("WORKSPACE_NOT_DIR", f"workspace 必须是目录: {p}")
    resolved = p.resolve()

    # 盘符根 / 文件系统根：两种模式都拒绝
    if str(resolved) == resolved.anchor or not resolved.name:
        raise FileToolError("WORKSPACE_TOO_BROAD", f"不能把 workspace 设成根目录: {resolved}")

    if not strict:
        return resolved

    home = Path.home().resolve()
    if resolved == home:
        raise FileToolError("WORKSPACE_TOO_BROAD", "不能把 workspace 设成用户主目录")

    project = Path(BASE_DIR).resolve()
    if resolved == project or resolved in project.parents:
        raise FileToolError(
            "WORKSPACE_TOO_BROAD",
            "不能把 workspace 设成项目目录或其上级目录（会允许模型改写本项目源码）",
        )

    if (resolved / ".git").exists():
        raise FileToolError(
            "WORKSPACE_IS_REPO",
            "不能把 workspace 设成 git 仓库根目录；请指向仓库内某个子目录",
        )

    return resolved


def use_workspace(raw: str | os.PathLike | None, *, strict: bool = True) -> Path:
    """校验并切换 workspace；``None`` 表示恢复默认目录。

    每次运行开始时都应调用一次（即便传 None），以保证上一轮的自定义目录不会残留。
    ``strict`` 的语义见 :func:`validate_workspace`。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return set_workspace_root(None)
    return set_workspace_root(validate_workspace(raw, strict=strict))


def workspace_root() -> Path:
    return WORKSPACE_ROOT


# ---------------------------------------------------------------------------
# 路径校验
# ---------------------------------------------------------------------------
def _safe_path(raw_path: str, *, for_write: bool) -> tuple[Path, str]:
    """把用户给出的相对路径解析为 workspace 内的绝对路径，并做全部安全校验。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise FileToolError("INVALID_ARGUMENT", "path 必须是非空字符串")
    if len(raw_path) > 240:
        raise FileToolError("INVALID_ARGUMENT", "path 不能超过 240 字符")

    normalized = raw_path.replace("\\", "/").strip()
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or Path(normalized).is_absolute():
        raise FileToolError("PATH_NOT_ALLOWED", "只允许 workspace 内的相对路径")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise FileToolError("PATH_NOT_ALLOWED", "路径不能包含空段、. 或 ..")
    for part in pure.parts:
        if any(ch in part for ch in _BAD_CHARS) or part.rstrip(" .") != part:
            raise FileToolError("PATH_NOT_ALLOWED", "路径包含文件名不允许的字符")
        if part.split(".", 1)[0].upper() in WINDOWS_RESERVED:
            raise FileToolError("PATH_NOT_ALLOWED", "路径使用了 Windows 保留名称")
    if pure.suffix.lower() not in ALLOWED_SUFFIXES:
        raise FileToolError(
            "UNSUPPORTED_FILE_TYPE",
            "仅支持文本文件: " + ", ".join(sorted(ALLOWED_SUFFIXES)),
        )

    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    candidate = (WORKSPACE_ROOT / Path(*pure.parts)).resolve()
    if candidate != WORKSPACE_ROOT and not candidate.is_relative_to(WORKSPACE_ROOT):
        raise FileToolError("PATH_NOT_ALLOWED", "解析后的路径超出了 workspace")
    if candidate == WORKSPACE_ROOT:
        raise FileToolError("PATH_NOT_ALLOWED", "path 必须指向文件")
    if candidate.is_symlink():
        raise FileToolError("PATH_NOT_ALLOWED", "不允许通过符号链接访问文件")
    if for_write:
        cur = candidate.parent
        while cur != WORKSPACE_ROOT and cur != cur.parent:
            if cur.is_symlink():
                raise FileToolError("PATH_NOT_ALLOWED", "不允许通过符号链接目录写入")
            cur = cur.parent
    return candidate, pure.as_posix()


# ---------------------------------------------------------------------------
# 实现层
# ---------------------------------------------------------------------------
def read_file_impl(path: str, max_chars: int = 20000) -> dict:
    """读取 workspace 内的 UTF-8 文本文件。"""
    lo, hi = MAX_CHARS_RANGE
    if type(max_chars) is not int or not lo <= max_chars <= hi:
        raise FileToolError("INVALID_ARGUMENT", f"max_chars 必须为 {lo}~{hi} 的整数")

    target, display = _safe_path(path, for_write=False)
    if not target.is_file():
        raise FileToolError("FILE_NOT_FOUND", "workspace 中不存在该文件")

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        raise FileToolError("FILE_TOO_LARGE", f"文件超过 {MAX_READ_BYTES} 字节限制")

    raw = target.read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileToolError("UNSUPPORTED_ENCODING", "文件不是有效的 UTF-8 文本") from exc

    return {
        "ok": True,
        "path": display,
        "content": content[:max_chars],
        "truncated": len(content) > max_chars,
        "bytes_read": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def write_file_impl(path: str, content: str, overwrite: bool = False) -> dict:
    """写入 workspace 内的文本文件（原子写，默认不覆盖）。"""
    if not isinstance(content, str):
        raise FileToolError("INVALID_ARGUMENT", "content 必须是字符串")
    if len(content) > MAX_WRITE_CHARS:
        raise FileToolError("CONTENT_TOO_LARGE", f"content 不能超过 {MAX_WRITE_CHARS} 字符")
    if type(overwrite) is not bool:
        raise FileToolError("INVALID_ARGUMENT", "overwrite 必须是布尔值")

    target, display = _safe_path(path, for_write=True)
    if target.exists() and not overwrite:
        raise FileToolError("FILE_EXISTS", "文件已存在；如需覆盖必须设置 overwrite=True")

    target.parent.mkdir(parents=True, exist_ok=True)
    raw = content.encode("utf-8")
    existed = target.exists()
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                                         prefix=".write-", suffix=".tmp",
                                         delete=False) as fh:
            temp_name = fh.name
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, target)
        temp_name = None
    except OSError as exc:
        raise FileToolError("WRITE_ERROR", f"写入失败: {type(exc).__name__}") from exc
    finally:
        if temp_name and Path(temp_name).exists():
            Path(temp_name).unlink(missing_ok=True)

    return {
        "ok": True,
        "path": display,
        "bytes_written": len(raw),
        "overwritten": existed,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _to_json(fn, *args, **kwargs) -> str:
    """执行实现函数并把结果/错误统一转成 JSON 字符串。"""
    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except FileToolError as exc:
        return json.dumps({"ok": False, "code": exc.code, "message": exc.message},
                          ensure_ascii=False)
    except Exception as exc:                      # noqa: BLE001 - 兜底不外抛
        return json.dumps({"ok": False, "code": "INTERNAL_ERROR",
                           "message": f"{type(exc).__name__}: {exc}"},
                          ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
@tool
def read_file(path: str, max_chars: int = 20000) -> str:
    """读取项目 workspace 内的 UTF-8 文本文件，返回内容与 sha256。
    路径必须为 workspace 内的相对路径，且为白名单内的文本后缀。"""
    return _to_json(read_file_impl, path, max_chars)


@tool
def write_file(path: str, content: str, overwrite: bool = False) -> str:
    """写入项目 workspace 内的文本文件（原子写）。
    默认不覆盖已有文件；路径必须为 workspace 内的相对路径。"""
    return _to_json(write_file_impl, path, content, overwrite)


FILE_TOOLS = [read_file, write_file]


# ---------------------------------------------------------------------------
# 辅助：列出 workspace 内的文件 / 读取 sources 目录
# ---------------------------------------------------------------------------
def list_workspace_files() -> list[dict]:
    """列出 workspace 内的文件（相对路径、字节数），供界面展示。"""
    root = WORKSPACE_ROOT
    if not root.exists():
        return []
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        if p.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        try:
            out.append({"path": p.relative_to(root).as_posix(),
                        "bytes": p.stat().st_size})
        except OSError:
            continue
    return out


def collect_sources(max_files: int = 3, max_total_chars: int = 6000) -> dict:
    """把 ``workspace/sources/`` 下的文本逐个读入，作为统一任务的补充材料。"""
    root = WORKSPACE_ROOT
    src_dir = root / "sources"
    if not src_dir.is_dir():
        return {"ok": True, "files": [], "text": ""}

    picked, texts, total = [], [], 0
    for p in sorted(src_dir.rglob("*")):
        if len(picked) >= max_files or total >= max_total_chars:
            break
        if not p.is_file() or p.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        rel = p.relative_to(root).as_posix()
        try:
            res = read_file_impl(rel, max_chars=min(30000, max_total_chars - total))
        except FileToolError:
            continue
        if not res.get("ok"):
            continue
        picked.append(rel)
        texts.append(f"### {rel}\n{res['content']}")
        total += len(res["content"])

    return {"ok": True, "files": picked, "text": "\n\n".join(texts)}


_SAFE_SLUG = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff\-_]+")


def safe_filename(name: str, fallback: str = "run") -> str:
    """把主题等自由文本压成安全文件名片段。"""
    slug = _SAFE_SLUG.sub("-", (name or "").strip()).strip("-")
    return (slug or fallback)[:40]