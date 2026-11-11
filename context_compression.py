"""对话历史压缩：把过长历史折成「摘要 + 最近若干轮」。

**为什么需要**：本项目的两种实现都按"一次任务"设计，但一旦作为通用 UI 的后端被接入，
同一会话会不断追加历史。模型窗口是有限的，历史涨到接近上限时要么被服务端截断、要么直接报错。
压缩是纯后端行为——用户那一轮不该因此失败，最差也应该退回完整历史。

**策略**（与 agent-ui 的压缩语义保持一致，便于对照）：

1. 估算 token；未超过 ``threshold * window`` 就原样返回，一次模型调用都不发；
2. 超过时把"最近 ``keep_rounds`` 轮"原样保留，更早的部分交给模型压成一段摘要；
3. 摘要调模型失败时退回**本地摘要**（截取每条的要点），保证这一轮照常进行；
4. 摘要按会话增量维护：``existing_summary`` + 尚未覆盖的 ``through`` 之后的部分，
   避免每轮把整段历史重压一遍。

**注意**：这里只做"历史压缩"，不碰工具回执与 system prompt——那些属于调用方的编排。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("langgraph_brief")

#: 模型上下文窗口（token）。0 表示不压缩。可用环境变量覆盖。
CONTEXT_WINDOW_ENV = "LANGGRAPH_BRIEF_CONTEXT_WINDOW"

#: 缺省窗口。DeepSeek 系常见 64K，取它做保守默认值。
DEFAULT_CONTEXT_WINDOW = 64_000

#: 触发压缩的占用比例
COMPRESS_THRESHOLD = 0.75

#: 原样保留的最近轮数（1 轮 = user + assistant 两条）
KEEP_RECENT_ROUNDS = 6

#: 摘要正文的目标长度（中文字符）
SUMMARY_TARGET_CHARS = 300

#: 摘要消息的固定前缀，便于模型和人一眼看出这不是用户原话
SUMMARY_PREFIX = "【以下是本次会话早前内容的摘要，供你参考】"

_SUMMARY_PROMPT = """你在为一段较长的对话做上下文压缩。请把下面的对话记录压成一段摘要。

要求：
- 保留：用户的目标与约束、已确认的事实与结论、未完成的事项、明确的偏好与取舍；
- 丢弃：寒暄、重复表述、已被推翻的中间尝试；
- 用第三人称陈述，不要复述原文句子；
- 不超过 {target} 字，直接输出摘要正文，不要加标题或前后缀。

{existing}对话记录：
{transcript}"""


@dataclass
class CompressionResult:
    """一次压缩的结果，便于调用方记录与展示。"""

    compressed: bool = False
    #: ``none``（未触发）/ ``llm``（模型摘要）/ ``local``（本地降级摘要）
    method: str = "none"
    before_tokens: int = 0
    after_tokens: int = 0
    dropped_messages: int = 0
    summary: str = ""
    #: 摘要已覆盖到原消息列表的第几条（不含），用于下一轮增量压缩
    through: int = 0
    #: 未触发压缩的原因，或降级原因
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "compressed": self.compressed,
            "method": self.method,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "dropped_messages": self.dropped_messages,
            "through": self.through,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------
_ENCODER = None


def _encoder():
    """尽量用 tiktoken 精确估算；不可用时退回字符估算。"""
    global _ENCODER
    if _ENCODER is None:
        try:
            import tiktoken

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:                       # 没装或没网下载词表
            _ENCODER = False
    return _ENCODER or None


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数。

    有 tiktoken 时按 cl100k_base 编码；否则按"中日韩字符 1 token、其余 4 字符 1 token"
    粗估——对"判断有没有超过窗口的 75%"这个用途足够了。
    """
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def count_tokens(messages: list[dict[str, Any]]) -> int:
    """估算整个消息列表的 token 数（含每条固定的角色开销）。"""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):           # 多模态块
            content = " ".join(
                str(c.get("text", "")) for c in content if isinstance(c, dict)
            )
        total += estimate_tokens(str(content or "")) + 4
    return total


def resolve_window(explicit: int | None = None) -> int:
    """确定窗口大小：显式传入 > 环境变量 > 默认值。0 表示**关闭压缩**。

    ``explicit=0`` 是合法输入（明确要求不压缩），所以不能用 ``if explicit:`` 判断——
    0 是 falsy，会被误当成"没传"而回落到默认值。
    """
    if explicit is not None:
        return max(0, int(explicit))
    raw = os.environ.get(CONTEXT_WINDOW_ENV, "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning("%s=%r 不是整数，改用默认 %d", CONTEXT_WINDOW_ENV, raw,
                           DEFAULT_CONTEXT_WINDOW)
    return DEFAULT_CONTEXT_WINDOW


# ---------------------------------------------------------------------------
# 压缩
# ---------------------------------------------------------------------------
def _transcript(messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        role = "用户" if m.get("role") == "user" else "助手"
        content = str(m.get("content") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _local_summary(messages: list[dict[str, Any]], limit: int = SUMMARY_TARGET_CHARS) -> str:
    """模型不可用时的降级摘要：每条取开头，按预算均分。"""
    if not messages:
        return ""
    per = max(24, limit // max(1, len(messages)))
    parts = []
    for m in messages:
        role = "用户" if m.get("role") == "user" else "助手"
        content = str(m.get("content") or "").strip().replace("\n", " ")
        if not content:
            continue
        parts.append(f"{role}：{content[:per]}")
    return "\n".join(parts)[: limit * 2]


def _summarize(older: list[dict[str, Any]], llm, existing_summary: str | None) -> tuple[str, str]:
    """返回 (摘要, 方式)。方式为 ``llm`` 或 ``local``。"""
    transcript = _transcript(older)
    if not transcript:
        return (existing_summary or ""), "local"
    if llm is not None:
        existing = (
            f"已有摘要（请与下面的新内容合并成一段新的摘要）：\n{existing_summary}\n\n"
            if existing_summary else ""
        )
        prompt = _SUMMARY_PROMPT.format(
            target=SUMMARY_TARGET_CHARS, existing=existing, transcript=transcript
        )
        try:
            out = llm.invoke(prompt)
            text = getattr(out, "content", out)
            if isinstance(text, list):
                text = " ".join(str(c.get("text", "")) for c in text if isinstance(c, dict))
            text = str(text or "").strip()
            if text:
                return text, "llm"
            logger.warning("压缩模型返回空摘要，改用本地摘要")
        except Exception as exc:                # noqa: BLE001 - 压缩失败不能拖垮这一轮
            logger.warning("压缩模型调用失败(%s: %s)，改用本地摘要",
                           type(exc).__name__, exc)
    local = _local_summary(older)
    if existing_summary:
        local = f"{existing_summary}\n{local}"
    return local, "local"


def compress_history(
    messages: list[dict[str, Any]],
    llm=None,
    *,
    window: int | None = None,
    threshold: float = COMPRESS_THRESHOLD,
    keep_rounds: int = KEEP_RECENT_ROUNDS,
    existing_summary: str | None = None,
    through: int = 0,
) -> tuple[list[dict[str, Any]], CompressionResult]:
    """按需压缩历史，返回 ``(新消息列表, 结果)``。

    ``existing_summary`` / ``through`` 用于**增量压缩**：上一轮已经压到第 ``through`` 条，
    这一轮只需要处理它之后新增的部分，不必把整段历史重新摘一遍。

    ``llm`` 为 ``None`` 时直接用本地摘要（测试与不支持调用模型的调用方）。
    """
    window = resolve_window(window)
    before = count_tokens(messages)
    result = CompressionResult(before_tokens=before, after_tokens=before)

    if window <= 0:
        result.note = "未启用（窗口=0）"
        return messages, result
    budget = int(window * threshold)
    if before <= budget:
        result.note = f"未触发（{before} ≤ {budget}）"
        return messages, result

    keep = max(2, keep_rounds * 2)
    if len(messages) <= keep:
        # 历史本身不长，只是单条太长：压缩帮不上忙，交给调用方
        result.note = f"未压缩（历史仅 {len(messages)} 条，不足以切分）"
        return messages, result

    older, recent = messages[:-keep], messages[-keep:]
    # 增量：跳过已被上一轮摘要覆盖的部分
    pending = older[max(0, min(through, len(older))):]
    if pending or not existing_summary:
        summary, method = _summarize(pending or older, llm, existing_summary)
    else:
        summary, method = existing_summary, "llm"

    if not summary.strip():
        result.note = "未压缩（摘要为空）"
        return messages, result

    head = {"role": "user", "content": f"{SUMMARY_PREFIX}\n{summary.strip()}"}
    new_messages = [head, *recent]

    result.compressed = True
    result.method = method
    result.summary = summary.strip()
    result.through = len(older)
    result.dropped_messages = len(older)
    result.after_tokens = count_tokens(new_messages)
    result.note = (
        f"{before} → {result.after_tokens} token"
        f"（压缩 {len(older)} 条，保留最近 {keep_rounds} 轮，方式={method}）"
    )
    return new_messages, result


__all__ = [
    "COMPRESS_THRESHOLD",
    "CONTEXT_WINDOW_ENV",
    "CompressionResult",
    "DEFAULT_CONTEXT_WINDOW",
    "KEEP_RECENT_ROUNDS",
    "SUMMARY_PREFIX",
    "compress_history",
    "count_tokens",
    "estimate_tokens",
    "resolve_window",
]