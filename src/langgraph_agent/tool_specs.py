"""内置工具的**纯数据**清单。

只放名字和描述，不 import 任何实现——这样 UI 列工具目录时是毫秒级，
不会因为加载 requests / lxml / MCP 客户端而卡住首屏。

对齐 agent-ui 的契约：``AgentBackend.tool_catalog()`` 返回
``(name, description)``，``RunRequest.tools.disabled`` 给出这一轮关掉的名字。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BuiltinToolSpec:
    """一个内置工具的声明。"""

    name: str
    description: str
    #: 没有 workspace 时跳过（写文件这类必须有落点）
    requires_workspace: bool = False
    #: 需要可读路径（workspace 或本轮附件）才给
    requires_read_paths: bool = False
    #: 默认开关状态（UI 里显示的开/关）
    enabled_by_default: bool = True


#: 三个基线工具 + 三个文件工具。命令执行不在内核里，由宿主按需注入。
TOOL_SPECS: tuple[BuiltinToolSpec, ...] = (
    BuiltinToolSpec("search_web", "搜索公开网页并返回候选来源（标题、链接、摘要）。"),
    BuiltinToolSpec("read_webpage", "读取公开网页正文，返回纯文本。"),
    BuiltinToolSpec("calculator", "执行受控算术表达式，只支持数值与 + - * / ** % 括号。"),
    BuiltinToolSpec(
        "read_file",
        "读取当前工作空间或本轮附件里的文件。",
        requires_read_paths=True,
    ),
    BuiltinToolSpec(
        "write_file",
        "在当前工作空间内写入文件。",
        requires_workspace=True,
    ),
    BuiltinToolSpec(
        "list_directory",
        "列出当前工作空间或附件目录的内容。",
        requires_read_paths=True,
    ),
    BuiltinToolSpec(
        "browser_dom",
        "用本机 headless 浏览器打开页面，返回渲染后的 DOM 与控制台报错（JSON）。",
        requires_workspace=True,
    ),
    BuiltinToolSpec(
        "browser_screenshot",
        "用本机 headless 浏览器截图并保存到工作目录，返回文件路径。",
        requires_workspace=True,
    ),
    BuiltinToolSpec(
        "shell",
        "在工作空间内执行一条只读命令（白名单：where/which、python --version、"
        "pip list、git status 等），返回退出码与输出。非版本查询需用户确认。",
        requires_workspace=True,
        enabled_by_default=False,      # 风险最高，默认不给模型
    ),
)

TOOL_NAMES = frozenset(spec.name for spec in TOOL_SPECS)
SPEC_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


def catalog() -> tuple[tuple[str, str, bool], ...]:
    """给 UI 的目录：(名字, 描述, 默认是否启用)。"""
    return tuple((s.name, s.description, s.enabled_by_default) for s in TOOL_SPECS)


__all__ = ["BuiltinToolSpec", "SPEC_BY_NAME", "TOOL_NAMES", "TOOL_SPECS", "catalog"]