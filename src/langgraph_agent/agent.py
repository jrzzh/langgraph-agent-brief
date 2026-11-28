"""通用 agent 内核：一个「模型 + 工具 + 技能目录」驱动的 ReAct 循环。

**只管循环本身**，不关心工具从哪来、技能是什么内容、检索走哪个服务：

- 工具由调用方以对象列表注入（``tools=[...]``）；
- 技能目录由调用方给路径（``skills_dir=...``），内核负责挂载与声明；
- 应用入口（CLI / Streamlit / 桌面 UI）各自决定注入什么。

因此这个模块**不 import** 任何 MCP 客户端、HTTP 抓取库、UI 框架或业务模块；
它只依赖 ``deepagents`` / ``langchain`` / ``langgraph``。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from deepagents import create_deep_agent
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from .sandbox import LocalShellBackend
from langchain_openai import ChatOpenAI

logger = logging.getLogger("langgraph_agent")

# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
DEFAULT_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
DEFAULT_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_TEMPERATURE = 0.3
TEMPERATURE_MIN, TEMPERATURE_MAX = 0.0, 2.0
MAX_TOKENS_MIN, MAX_TOKENS_MAX = 1, 200_000

#: 单次模型请求的超时与重试。默认值的组合（30s × 2）最坏 60 秒；
#: 早期用 60s × 3 时，一次网络卡顿能让单步等到 180 秒。
LLM_TIMEOUT = float(os.environ.get("LANGGRAPH_BRIEF_LLM_TIMEOUT", "30"))
LLM_MAX_RETRIES = int(os.environ.get("LANGGRAPH_BRIEF_LLM_RETRIES", "1"))

#: 缺密钥时的占位值：保证 import 与构造不炸，真正的失败推迟到第一次请求。
#: 这样外部 UI 可以懒加载本包而不必先配环境变量。
_PLACEHOLDER_KEY = "not-configured"


def build_llm(
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """构造一个模型实例。

    连接信息优先用显式传入的值，其次读环境变量（``DEEPSEEK_*``）。
    """
    t = DEFAULT_TEMPERATURE if temperature is None else float(temperature)
    if not (TEMPERATURE_MIN <= t <= TEMPERATURE_MAX):
        raise ValueError(f"temperature 必须在 {TEMPERATURE_MIN}~{TEMPERATURE_MAX} 之间，收到 {t}")

    kwargs: dict[str, Any] = dict(
        model=model or DEFAULT_MODEL,
        api_key=api_key or os.environ.get("DEEPSEEK_API_KEY") or _PLACEHOLDER_KEY,
        base_url=base_url or DEFAULT_BASE_URL,
        temperature=t,
        timeout=LLM_TIMEOUT,
        max_retries=LLM_MAX_RETRIES,
    )
    if max_tokens is not None:
        n = int(max_tokens)
        if not (MAX_TOKENS_MIN <= n <= MAX_TOKENS_MAX):
            raise ValueError(
                f"max_tokens 必须在 {MAX_TOKENS_MIN}~{MAX_TOKENS_MAX} 之间，收到 {n}"
            )
        kwargs["max_tokens"] = n
    return ChatOpenAI(**kwargs)


# ---------------------------------------------------------------------------
# 系统提示
# ---------------------------------------------------------------------------
#: 默认系统提示。deepagents 会把它**追加在**自己的基础提示之前，
#: 因此技能机制照常工作，这里只约束"怎么说话"。
DEFAULT_SYSTEM_PROMPT = """\
你是技术调研助手。回答时务必遵守：

- **直接给出最终答案**，不要叙述计划和过程（例如"我先读取技能说明""接下来我会检索"），
  也不要汇报你读了哪些技能、调用了哪些工具；
- 不要整段复述工具返回的原文，只提炼与问题相关的结论；
- **检索要克制**：搜索不超过 4 次、抓取网页不超过 3 个；信息够写就立刻动笔，
  不要为了"更全面"反复检索——每次检索都要用户多等好几秒；
- 需要检索就直接调用工具，拿到结果后径直作答，不要边做边解说。
"""

#: 技能在后端里的挂载前缀。deepagents 要求正斜杠。
SKILLS_MOUNT = "/skills/"


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
def build_agent(
    llm=None,
    *,
    tools: Sequence[Any] | Iterable[Any] = (),
    skills_dir: str | os.PathLike | None = None,
    workspace: str | os.PathLike | None = None,
    exec_policy=None,
    exec_timeout: int | None = None,
    system_prompt: str | None = None,
    enable_execute: bool = True,
    recursion_limit: int | None = None,
):
    """构造一个可直接 ``.stream()`` 的 ReAct 图。

    ``tools``
        工具对象列表（任意 LangChain 工具）。内核不提供默认工具——
        检索、抓取、文件、命令全部由调用方注入。
    ``skills_dir``
        技能根目录：其下每个子目录放一个 ``SKILL.md``。给了就挂到 ``/skills/``
        并声明给模型；``None`` 表示不使用技能。
    ``workspace``
        文件系统的根。模型看到的 ``/`` 就是这个目录；``None`` 时用当前工作目录。
    ``system_prompt``
        ``None`` 用 :data:`DEFAULT_SYSTEM_PROMPT`；空串表示不加自定义提示。

    返回编译后的图，调用方按
    ``agent.stream({"messages": [...]}, stream_mode=["messages", "updates"])``
    即可同时拿到 token 流与节点更新。
    """
    root = Path(workspace).expanduser().resolve() if workspace else Path.cwd()
    root.mkdir(parents=True, exist_ok=True)

    # 用带执行能力的后端：deepagents 的 execute 工具只有在 backend 实现
    # SandboxBackendProtocol 时才真正可用，纯 FilesystemBackend 会每次都返回
    # "Execution not available"。enable_execute=False 可退回纯文件模式。
    if enable_execute:
        workspace_backend: Any = LocalShellBackend(
            root_dir=str(root),
            virtual_mode=True,
            policy=exec_policy,
            timeout=exec_timeout,
        )
    else:
        workspace_backend = FilesystemBackend(root_dir=str(root), virtual_mode=True)

    route: dict[str, Any] = {}
    if skills_dir is not None:
        skills_path = Path(skills_dir).expanduser().resolve()
        if not skills_path.is_dir():
            raise ValueError(f"skills_dir 不是目录：{skills_path}")
        # CompositeBackend **会剥掉前缀再委派**：/skills/x 在目标后端上就是 x，
        # 所以路由目标必须指向技能根目录本身，而不是它的上层。
        route[SKILLS_MOUNT] = FilesystemBackend(root_dir=str(skills_path), virtual_mode=True)

    backend = (
        CompositeBackend(default=workspace_backend, routes=route) if route
        else workspace_backend
    )

    kwargs: dict[str, Any] = dict(
        model=llm if llm is not None else build_llm(),
        tools=list(tools),
        backend=backend,
        system_prompt=(DEFAULT_SYSTEM_PROMPT if system_prompt is None else system_prompt) or None,
    )
    if route:
        kwargs["skills"] = [SKILLS_MOUNT]

    logger.debug(
        "build_agent: tools=%s skills=%s workspace=%s",
        [getattr(t, "name", t) for t in kwargs["tools"]],
        bool(route), root,
    )
    return create_deep_agent(**kwargs)


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_TEMPERATURE",
    "LLM_MAX_RETRIES",
    "LLM_TIMEOUT",
    "MAX_TOKENS_MAX",
    "MAX_TOKENS_MIN",
    "SKILLS_MOUNT",
    "TEMPERATURE_MAX",
    "TEMPERATURE_MIN",
    "build_agent",
    "build_llm",
]
