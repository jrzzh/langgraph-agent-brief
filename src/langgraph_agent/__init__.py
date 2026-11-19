"""langgraph_agent —— 通用 agent 内核（ReAct 循环）。

这个包只做一件事：把「模型 + 工具 + 技能目录」拼成一个可流式的图。
工具、技能、检索服务全部由调用方注入，内核不含任何业务依赖。

典型用法::

    from langgraph_agent import build_llm, build_agent

    llm = build_llm(model="deepseek-chat", api_key="sk-...")
    agent = build_agent(
        llm,
        tools=my_tools,                 # 外部注入
        skills_dir="./skills",          # 外部注入
        workspace="./workspace",
    )
    for mode, payload in agent.stream({"messages": [...]},
                                      stream_mode=["messages", "updates"]):
        ...

**导入是惰性的**：``import langgraph_agent`` 不会立刻把 ``deepagents`` 拉进进程。
通用 UI 启动时只需要工具目录（``langgraph_agent.tool_specs``，纯数据、毫秒级），
等真正要跑一轮时再触达循环本体。
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.1.0"

#: 公开名字 → 所在子模块。只有真正取用时才 import，避免启动即加载 deepagents。
_LAZY: dict[str, str] = {
    "build_llm": "agent",
    "build_agent": "agent",
    "DEFAULT_MODEL": "agent",
    "DEFAULT_SYSTEM_PROMPT": "agent",
    "DEFAULT_TEMPERATURE": "agent",
    "SKILLS_MOUNT": "agent",
    "LLM_TIMEOUT": "agent",
    "LLM_MAX_RETRIES": "agent",
    "ToolBox": "runtime",
    "ToolSpec": "runtime",
}

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_TEMPERATURE",
    "LLM_MAX_RETRIES",
    "LLM_TIMEOUT",
    "SKILLS_MOUNT",
    "ToolBox",
    "ToolSpec",
    "build_agent",
    "build_llm",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value          # 缓存，后续访问不再走 __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(__all__)