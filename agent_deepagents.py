# -*- coding: utf-8 -*-
"""
统一任务 —— deepagents 官方原生 Skills 实现
================================================================
与 `agent_brief.py` 完成同一个统一任务（检索 → 交叉核验 → 技术简报），
但 **Skills 部分改用 LangChain 官方 `deepagents` 包的原生能力**：

    create_deep_agent(..., skills=["/skills/"])   ← 一行接入，无需自建加载器

采用 Agent Skills 规范的**三级渐进式披露**：
    1. Metadata    启动时把每个技能的 name / description 注入 system prompt
    2. Instructions 技能被判定相关时，模型用 `read_file` 读取完整 SKILL.md
    3. Resources   按需读取 skills/<name>/references、scripts、assets

复用 `agent_brief.py` 的 3 个工具（web_search / web_fetch / calculator，
其中前两个走千问 MCP 并有本地降级），因此工具层与本项目其余部分完全一致。

用法：
    python agent_deepagents.py [主题]
"""

import json
import os
import sys
import time
from datetime import datetime

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from agent_brief import BASE_DIR, OUT_DIR, TOOLS, llm

SKILLS_SOURCE = "/skills/"          # 相对 backend root，deepagents 要求正斜杠


TASK_TEMPLATE = """请完成一次技术简报任务，主题是「{topic}」。

步骤要求：
1) 联网检索该主题的资料（可用 web_search、web_fetch 工具）；
2) 提取 3-5 条关键信息，并对关键事实做交叉核验，逐条标注「多来源一致 / 单一来源 / 待核实」；
3) 输出一份中文 Markdown 技术简报：正文不超过 500 字，「来源」小节列出链接、不计入字数。

注意：如果可用技能（skills）与本次任务的某个步骤相关，请先读取对应技能的
SKILL.md 并遵循其中的方法要求，再开始执行。"""


def main() -> None:
    topic = sys.argv[1] if len(sys.argv) > 1 else "大模型上下文工程"
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_lines: list[str] = []

    def log(msg: str) -> None:
        print(msg, flush=True)
        log_lines.append(msg)

    log("========== deepagents 原生 Skills 实现 ==========")
    log(f"主题={topic} | 模型={os.environ.get('DEEPSEEK_MODEL')} | RUN_ID={run_id}")
    log(f"技能源={SKILLS_SOURCE} | backend=FilesystemBackend(virtual_mode=True，根目录=项目根)")

    # 技能目录只作为 backend 中的一个路径源；元信息由 SkillsMiddleware 在启动时扫描
    backend = FilesystemBackend(root_dir=BASE_DIR, virtual_mode=True)

    t0 = time.time()
    try:
        agent = create_deep_agent(
            model=llm,
            tools=TOOLS,
            backend=backend,
            skills=[SKILLS_SOURCE],
        )
        build_s = round(time.time() - t0, 2)
        log(f"agent 构建完成（含技能扫描）: {build_s}s")

        t1 = time.time()
        result = agent.invoke({"messages": [{"role": "user",
                                            "content": TASK_TEMPLATE.format(topic=topic)}]})
        elapsed = round(time.time() - t1, 2)
    except Exception as e:
        log(f"❌ 执行失败: {type(e).__name__}: {e}")
        raise

    msgs = result.get("messages", []) or []
    final = ""
    for m in reversed(msgs):
        c = getattr(m, "content", None)
        if isinstance(c, str) and c.strip():
            final = c
            break

    # 统计 agent 步数：助手消息里带 tool_calls 的次数
    steps = 0
    tool_names: list[str] = []
    tok_in = tok_out = 0
    for m in msgs:
        u = getattr(m, "usage_metadata", None) or {}
        tok_in += int(u.get("input_tokens", 0))
        tok_out += int(u.get("output_tokens", 0))
        for tc in (getattr(m, "tool_calls", None) or []):
            steps += 1
            nm = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
            if nm:
                tool_names.append(nm)

    log(f"总耗时 {elapsed}s | 消息数 {len(msgs)} | 工具调用 {steps} 次 | "
        f"token(in={tok_in}, out={tok_out}, 总={tok_in + tok_out})")
    if tool_names:
        from collections import Counter
        log(f"工具调用分布: {dict(Counter(tool_names))}")

    brief_path = os.path.join(OUT_DIR, f"brief_deepagents_{run_id}.md")
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(final)
    stat_path = os.path.join(OUT_DIR, f"stats_deepagents_{run_id}.json")
    with open(stat_path, "w", encoding="utf-8") as f:
        json.dump({
            "run_id": run_id,
            "impl": "deepagents(create_deep_agent + SkillsMiddleware)",
            "topic": topic,
            "model": os.environ.get("DEEPSEEK_MODEL"),
            "skills_source": SKILLS_SOURCE,
            "skills_disclosure": "3 级：metadata -> SKILL.md(read_file) -> resources",
            "build_elapsed_s": build_s,
            "run_elapsed_s": elapsed,
            "messages": len(msgs),
            "tool_calls": steps,
            "tool_name_counts": dict(__import__("collections").Counter(tool_names)),
            "read_file_calls": tool_names.count("read_file"),
            "token_input": tok_in,
            "token_output": tok_out,
            "token_total": tok_in + tok_out,
            "brief_chars": len(final),
            "log": log_lines,
        }, f, ensure_ascii=False, indent=2)

    log(f"简报已保存: output/brief_deepagents_{run_id}.md")
    log(f"统计已保存: output/stats_deepagents_{run_id}.json")
    print("\n===== 最终简报 =====\n")
    print(final)


if __name__ == "__main__":
    main()