"""工具注入：把"有哪些工具"与"这一轮启用哪些"分开。

内核不关心工具的实现，只认名字与描述。应用层（UI / 实验台）通过
:func:`catalog` 拿到可开关的工具清单，再用 :func:`select` 按启用名单挑出对象。

这与 agent-ui 的契约对齐：``tool_catalog()`` 返回 ``(name, description)`` 列表，
``RunRequest.tools.disabled`` 给出这一轮关掉的名字。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个可注入工具的声明。

    ``factory`` 接收本次的上下文（例如 workspace 路径），返回真正的工具对象；
    需要固定依赖的工具用 ``functools.partial`` 或闭包包住即可。
    """

    name: str
    description: str
    factory: Callable[[dict[str, Any]], Any] | None = None
    tool: Any = None
    #: 没有 workspace 时是否要跳过
    requires_workspace: bool = False
    #: 默认是否启用（UI 里显示为开/关）
    enabled_by_default: bool = True

    def build(self, context: dict[str, Any]) -> Any:
        if self.tool is not None:
            return self.tool
        if self.factory is None:
            raise ValueError(f"工具 {self.name!r} 既没有 tool 也没有 factory")
        return self.factory(context)


@dataclass
class ToolBox:
    """一组工具声明的集合。"""

    specs: tuple[ToolSpec, ...] = ()
    _by_name: dict[str, ToolSpec] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_name = {s.name: s for s in self.specs}

    @classmethod
    def of(cls, *specs: ToolSpec) -> "ToolBox":
        return cls(specs=tuple(specs))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.specs)

    def catalog(self) -> tuple[tuple[str, str, bool], ...]:
        """给 UI 的工具目录：(名字, 描述, 默认是否启用)。"""
        return tuple((s.name, s.description, s.enabled_by_default) for s in self.specs)

    def select(
        self,
        context: dict[str, Any],
        *,
        disabled: Iterable[str] = (),
        enabled: Sequence[str] | None = None,
    ) -> list[Any]:
        """按启用名单挑出工具对象。

        ``disabled`` 优先（与 agent-ui 的 ``RunRequest.tools.disabled`` 一致）；
        ``enabled`` 给定时只保留其中的名字。
        """
        off = set(disabled)
        unknown = off - set(self._by_name)
        if unknown:
            # 只告警不抛：UI 侧可能残留旧名字，不该因为一个开关崩掉整轮
            pass
        picked: list[Any] = []
        for spec in self.specs:
            if spec.name in off:
                continue
            if enabled is not None and spec.name not in enabled:
                continue
            if spec.requires_workspace and not context.get("workspace"):
                continue
            picked.append(spec.build(context))
        return picked


__all__ = ["ToolBox", "ToolSpec"]