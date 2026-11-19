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
"""

from __future__ import annotations

from .agent import (
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TEMPERATURE,
    SKILLS_MOUNT,
    build_agent,
    build_llm,
)
from .runtime import ToolBox, ToolSpec

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_TEMPERATURE",
    "SKILLS_MOUNT",
    "ToolBox",
    "ToolSpec",
    "build_agent",
    "build_llm",
]