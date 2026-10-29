# -*- coding: utf-8 -*-
"""
统一任务 —— deepagents 官方原生 Skills 实现（带日志与分节点计时）

与 `agent_brief.py` 完成同一个统一任务（检索 → 交叉核验 → 技术简报），
但 Skills 部分改用 LangChain 官方 `deepagents` 包的原生能力：

    create_deep_agent(..., skills=["/skills/"])   ← 一行接入，无需自建加载器

采用 Agent Skills 规范的**三级渐进式披露**：
    1. Metadata    启动时把每个技能的 name / description 注入 system prompt
    2. Instructions 技能被判定相关时，模型用 `read_file` 读取完整 SKILL.md
    3. Resources   按需读取 skills/<name>/references、scripts、assets

本脚本在此之上增加三件事：

1. **运行日志写入 logs/**：`logs/deepagents_<RUN_ID>.log`，同时打印到控制台；
2. **各节点耗时**：用 LangChain 回调统计每个图节点的累计耗时与占比、
   每次 LLM 调用的耗时与 token、每次工具调用的耗时与参数；
3. **实际加载了哪些技能**：从 `read_file` 工具调用的入参里解析 `skills/<名>/SKILL.md`。

说明：deepagents 是「模型自主循环」，没有 StateGraph 那样的固定节点，
所以「节点」= 图里实际执行的节点（model / tools / SkillsMiddleware.* 等）。

不用 astream_events 而用回调，是因为 `web_search` 内部调用 asyncio.run，
在已有事件循环里会报 "cannot be called from a running event loop"。

用法：
    python agent_deepagents.py [主题]
"""

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

from langchain_core.callbacks import BaseCallbackHandler

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from agent_brief import BASE_DIR, LOG_DIR, OUT_DIR, TOOLS, _rel, llm

SKILLS_SOURCE = "/skills/"          # 相对 backend root，deepagents 要求正斜杠
SKILLS_DIR = os.path.join(BASE_DIR, "skills")

# 从工具入参里解析技能路径：skills/<name>/SKILL.md（兼容正反斜杠）
SKILL_PATH_RE = re.compile(r"skills[/\\]([^/\\\"']+)[/\\]SKILL\.md", re.I)

TASK_TEMPLATE = """请完成一次技术简报任务，主题是「{topic}」。

步骤要求：
1) 联网检索该主题的资料（可用 web_search、web_fetch 工具）；
2) 提取 3-5 条关键信息，并对关键事实做交叉核验，逐条标注「多来源一致 / 单一来源 / 待核实」；
3) 输出一份中文 Markdown 技术简报：正文不超过 500 字，「来源」小节列出链接、不计入字数。

注意：如果可用技能（skills）与本次任务的某个步骤相关，请先读取对应技能的
SKILL.md 并遵循其中的方法要求，再开始执行。"""


# ---------------------------------------------------------------------------
# 回调：采集节点 / LLM / 工具的时间与入参
# ---------------------------------------------------------------------------
class TimingHandler(BaseCallbackHandler):
    """记录各节点、LLM 调用与工具调用的起止时间。"""

    def __init__(self) -> None:
        self.t0 = time.time()
        self._node_start = {}
        self.node_total = defaultdict(float)
        self.node_count = Counter()
        self._llm_start = {}
        self.llm_calls = []
        self._tool_start = {}
        self._tool_ids = {}          # run_id -> 工具名，用于识别嵌套调用
        self.tool_calls = []         # 顶层工具调用
        self.nested_tool_calls = []  # 嵌套调用（如 web_search 内部的 MCP search_pro）
        self.skills = []             # [(技能名, 路径, 耗时)]
        self._seen_skill = set()
        self.tool_names = {t.name for t in TOOLS}   # 用于排除"工具自身的链"
        self.skipped_chain = Counter()

    # -- 图节点 ------------------------------------------------------------
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                       tags=None, metadata=None, **kwargs) -> None:
        if parent_run_id in self._tool_ids:      # 工具内部的子链，不计入节点
            return
        rname = (serialized or {}).get("name")
        node = (metadata or {}).get("langgraph_node")
        # 每次工具调用本身也会产生一条 chain（名字=工具名），它不是图节点，排除
        if rname in self.tool_names and rname != node:
            self.skipped_chain[rname] += 1
            return
        if node:
            self._node_start[run_id] = (node, time.time())

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None,
                     tags=None, metadata=None, **kwargs) -> None:
        item = self._node_start.pop(run_id, None)
        if item:
            node, st = item
            self.node_total[node] += time.time() - st
            self.node_count[node] += 1

    # -- LLM ---------------------------------------------------------------
    def on_chat_model_start(self, serialized, messages, *, run_id,
                            parent_run_id=None, tags=None, metadata=None, **kwargs) -> None:
        self._llm_start[run_id] = time.time()

    def on_llm_end(self, response, *, run_id, parent_run_id=None, tags=None, **kwargs) -> None:
        st = self._llm_start.pop(run_id, None)
        if st is None:
            return
        in_tok = out_tok = 0
        llm_out = getattr(response, "llm_output", None) or {}
        usage = (llm_out.get("token_usage") or {}) if isinstance(llm_out, dict) else {}
        in_tok = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        out_tok = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        if not (in_tok or out_tok):
            for gens in (getattr(response, "generations", None) or []):
                for g in gens:
                    um = getattr(getattr(g, "message", None), "usage_metadata", None) or {}
                    in_tok += int(um.get("input_tokens", 0) or 0)
                    out_tok += int(um.get("output_tokens", 0) or 0)
        self.llm_calls.append({
            "start_s": round(st - self.t0, 2),
            "dur_s": round(time.time() - st, 2),
            "in_tok": int(in_tok),
            "out_tok": int(out_tok),
        })

    # -- 工具 --------------------------------------------------------------
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None,
                      tags=None, metadata=None, inputs=None, **kwargs) -> None:
        name = (serialized or {}).get("name") or (metadata or {}).get("tool_name") or "?"
        raw = input_str if input_str is not None else json.dumps(inputs or {}, ensure_ascii=False)
        nested = parent_run_id in self._tool_ids      # 父级也是工具 => 嵌套调用
        self._tool_ids[run_id] = name
        self._tool_start[run_id] = (name, time.time(), str(raw), nested)

    def on_tool_end(self, output, *, run_id, parent_run_id=None, tags=None, **kwargs) -> None:
        item = self._tool_start.pop(run_id, None)
        if not item:
            return
        name, st, raw, nested = item
        dur = round(time.time() - st, 2)
        try:
            out_len = len(output) if isinstance(output, str) else len(str(output))
        except Exception:
            out_len = 0
        rec = {
            "start_s": round(st - self.t0, 2),
            "dur_s": dur,
            "name": name,
            "input": raw[:200],
            "out_len": out_len,
            "nested": nested,
        }
        (self.nested_tool_calls if nested else self.tool_calls).append(rec)
        # 技能加载：read_file 的入参里含 skills/<name>/SKILL.md
        m = SKILL_PATH_RE.search(raw)
        if m and name.lower() in ("read_file", "readfile", "read"):
            if m.group(1) not in self._seen_skill:
                self._seen_skill.add(m.group(1))
                self.skills.append((m.group(1), f"/skills/{m.group(1)}/SKILL.md", dur))

    # -- 汇总 --------------------------------------------------------------
    def _build_steps(self) -> list:
        """按「一次 LLM 调用 + 该次产出的工具调用」切分步骤。

        deepagents 是模型自主循环，没有 StateGraph 那样的固定节点，
        按步骤分解才是准确的阶段耗时口径。
        """
        calls = sorted(self.llm_calls, key=lambda c: c["start_s"])
        tools = sorted(self.tool_calls, key=lambda c: c["start_s"])
        # 每个工具归属到「最近一次已结束的 LLM 调用」所在步骤。
        # 不用时间窗口，避免工具与下一次 LLM 调用起始时刻重叠（如 6.64s）时被丢掉。
        buckets = [[] for _ in calls]
        for t in tools:
            idx = -1
            for j, c in enumerate(calls):
                if c["start_s"] + c["dur_s"] <= t["start_s"] + 0.02:
                    idx = j
            if idx >= 0:
                buckets[idx].append(t)
        steps = []
        for i, c in enumerate(calls):
            mine = buckets[i]
            nxt = calls[i + 1]["start_s"] if i + 1 < len(calls) else None
            if mine:
                last_end = max(t["start_s"] + t["dur_s"] for t in mine)
                span = round((nxt if nxt is not None else last_end) - mine[0]["start_s"], 2)
            else:
                span = 0.0
            steps.append({
                "step": i + 1,
                "llm_s": c["dur_s"],
                "tools_s": span,
                "tools": [t["name"] for t in mine],
                "in_tok": c["in_tok"],
                "out_tok": c["out_tok"],
            })
        return steps

    def summary(self, wall_s: float, build_s: float) -> dict:
        llm_dur = round(sum(c["dur_s"] for c in self.llm_calls), 2)
        tool_dur = round(sum(c["dur_s"] for c in self.tool_calls), 2)
        return {
            "wall_elapsed_s": round(wall_s, 2),
            "build_elapsed_s": round(build_s, 2),
            "node_timings": {k: round(v, 2) for k, v in
                             sorted(self.node_total.items(), key=lambda x: -x[1])},
            "node_counts": dict(self.node_count),
            "llm_calls": len(self.llm_calls),
            "llm_elapsed_s": llm_dur,
            "token_input": sum(c["in_tok"] for c in self.llm_calls),
            "token_output": sum(c["out_tok"] for c in self.llm_calls),
            "tool_calls": len(self.tool_calls),
            "tool_elapsed_s": tool_dur,
            "tool_name_counts": dict(Counter(c["name"] for c in self.tool_calls)),
            "nested_tool_calls": len(self.nested_tool_calls),
            "nested_tool_name_counts": dict(Counter(c["name"] for c in self.nested_tool_calls)),
            "skills_loaded": [s[0] for s in self.skills],
            "steps": self._build_steps(),
            "skills_available": sorted(
                d for d in os.listdir(SKILLS_DIR)
                if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md"))
            ) if os.path.isdir(SKILLS_DIR) else [],
        }


def _fmt_row(cols, widths) -> str:
    out = []
    for i, (c, w) in enumerate(zip(cols, widths)):
        out.append(str(c).ljust(w) if i < len(widths) - 1 else str(c).rjust(w))
    return "  ".join(out)


def build_report(h: TimingHandler, s: dict, run_id: str, topic: str,
                 brief_chars: int) -> list:
    """生成日志正文（文件与控制台共用）。"""
    L = []
    total = s["wall_elapsed_s"] or 1e-9

    L.append("=" * 78)
    L.append("deepagents 原生 Skills 运行日志")
    L.append(f"RUN_ID  : {run_id}")
    L.append(f"主题    : {topic}")
    L.append(f"模型    : {os.environ.get('DEEPSEEK_MODEL')}")
    L.append(f"技能源  : {SKILLS_SOURCE}")
    L.append("backend : FilesystemBackend(virtual_mode=True，根目录=项目根)")
    L.append(f"agent 构建耗时: {s['build_elapsed_s']}s（含 SkillsMiddleware 扫描技能）")
    L.append("=" * 78)

    # ---- 技能加载 ----
    L.append("")
    L.append("【技能加载】由 read_file 工具调用解析")
    avail = s["skills_available"]
    if h.skills:
        for i, (name, path, dur) in enumerate(h.skills, 1):
            L.append(f"  {i}. {name:<24} {path:<44} {dur}s")
        not_loaded = [a for a in avail if a not in {x[0] for x in h.skills}]
        L.append(f"  合计加载 {len(h.skills)}/{len(avail)} 个技能"
                 + (f"；未加载: {', '.join(not_loaded)}" if not_loaded else "（全部）"))
    else:
        L.append("  ⚠️ 本次运行未读取任何 SKILL.md（模型判定无需技能）")

    # ---- 步骤分解（deepagents 无固定节点，按步骤计时）----
    L.append("")
    L.append("【步骤耗时分解】（每步 = 1 次 LLM 调用 + 该步产出的工具调用）")
    L.append(_fmt_row(["步骤", "LLM(s)", "工具阶段(s)", "输入tok", "输出tok", "该步工具"],
                      [6, 9, 12, 10, 10, 42]))
    L.append("  " + "-" * 92)
    for st in s["steps"]:
        names = Counter(st["tools"])
        detail = "、".join(f"{k}×{v}" for k, v in names.items()) or "—（直接给出最终回答）"
        L.append(_fmt_row([st["step"], f"{st['llm_s']:.2f}", f"{st['tools_s']:.2f}",
                           st["in_tok"], st["out_tok"], detail], [6, 9, 12, 10, 10, 42]))
    L.append(f"  小计：LLM {s['llm_elapsed_s']}s + 工具阶段 "
             f"{round(sum(x['tools_s'] for x in s['steps']), 2)}s")
    L.append("")
    L.append("  中间件耗时（技能扫描/注入，远小于 LLM）：")
    for node, dur in s["node_timings"].items():
        if "Middleware" in node:
            L.append(f"    {node:<38} {s['node_counts'].get(node, 0)} 次  {dur:.3f}s")

    # ---- LLM 明细 ----
    L.append("")
    L.append("【LLM 调用明细】")
    L.append(_fmt_row(["#", "开始(s)", "耗时(s)", "输入token", "输出token"],
                      [4, 9, 9, 11, 11]))
    L.append("  " + "-" * 48)
    for i, c in enumerate(h.llm_calls, 1):
        L.append(_fmt_row([i, f"{c['start_s']:.2f}", f"{c['dur_s']:.2f}",
                           c["in_tok"], c["out_tok"]], [4, 9, 9, 11, 11]))

    # ---- 工具明细 ----
    L.append("")
    L.append("【工具调用明细】")
    L.append(_fmt_row(["#", "开始(s)", "耗时(s)", "工具", "输出", "参数摘要"],
                      [4, 9, 9, 12, 7, 40]))
    L.append("  " + "-" * 84)
    for i, c in enumerate(h.tool_calls, 1):
        arg = c["input"].replace("\n", " ")
        L.append(_fmt_row([i, f"{c['start_s']:.2f}", f"{c['dur_s']:.2f}",
                           c["name"], c["out_len"], arg[:40]], [4, 9, 9, 12, 7, 40]))

    # ---- 汇总 ----
    L.append("")
    L.append("【汇总】")
    L.append(f"  墙钟总耗时 : {s['wall_elapsed_s']}s（含 agent 构建 {s['build_elapsed_s']}s）")
    L.append(f"  LLM 调用   : {s['llm_calls']} 次  {s['llm_elapsed_s']}s"
             f"  ({s['llm_elapsed_s'] / total * 100:.1f}%)"
             f"  token in={s['token_input']} out={s['token_output']}"
             f" 总={s['token_input'] + s['token_output']}")
    L.append(f"  工具调用   : {s['tool_calls']} 次（顶层）  {s['tool_elapsed_s']}s"
             f"  {s['tool_name_counts']}")
    if s.get("nested_tool_calls"):
        L.append(f"  嵌套调用   : {s['nested_tool_calls']} 次"
                 f"  {s['nested_tool_name_counts']}"
                 f"（web_search/web_fetch 内部的 MCP 工具，已排除避免重复计数）")
    L.append(f"  技能加载   : {len(s['skills_loaded'])}/{len(avail)} 个  {s['skills_loaded']}")
    L.append(f"  简报字数   : {brief_chars}")
    L.append("=" * 78)
    return L


def main() -> None:
    topic = sys.argv[1] if len(sys.argv) > 1 else "大模型上下文工程"
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(LOG_DIR, exist_ok=True)

    log_path = os.path.join(LOG_DIR, f"deepagents_{run_id}.log")
    log_fh = open(log_path, "w", encoding="utf-8")

    def emit(lines) -> None:
        for ln in lines:
            print(ln, flush=True)
            log_fh.write(ln + "\n")
        log_fh.flush()

    emit([f"========== deepagents 原生 Skills 启动 RUN_ID={run_id} ==========",
          f"主题={topic} | 模型={os.environ.get('DEEPSEEK_MODEL')} | 技能源={SKILLS_SOURCE}"])

    # ---- 构建 ----
    t_build = time.time()
    backend = FilesystemBackend(root_dir=BASE_DIR, virtual_mode=True)
    agent = create_deep_agent(
        model=llm,
        tools=TOOLS,
        backend=backend,
        skills=[SKILLS_SOURCE],
    )
    build_s = round(time.time() - t_build, 2)

    # ---- 执行（回调采集）----
    handler = TimingHandler()
    handler.t0 = time.time()
    t0 = time.time()
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": TASK_TEMPLATE.format(topic=topic)}]},
            config={"callbacks": [handler]},
        )
        wall_s = round(time.time() - t0, 2)
    except Exception as e:
        emit([f"❌ 执行失败: {type(e).__name__}: {e}"])
        log_fh.close()
        raise

    msgs = result.get("messages", []) or []
    final = ""
    for m in reversed(msgs):
        c = getattr(m, "content", None)
        if isinstance(c, str) and c.strip():
            final = c
            break

    s = handler.summary(wall_s, build_s)
    emit([""])
    emit(build_report(handler, s, run_id, topic, len(final)))

    # ---- 落盘 ----
    brief_path = os.path.join(OUT_DIR, f"brief_deepagents_{run_id}.md")
    with open(brief_path, "w", encoding="utf-8") as f:
        f.write(final)
    stat = {
        "run_id": run_id,
        "impl": "deepagents(create_deep_agent + SkillsMiddleware)",
        "topic": topic,
        "model": os.environ.get("DEEPSEEK_MODEL"),
        "skills_source": SKILLS_SOURCE,
        "skills_disclosure": "3 级：metadata -> SKILL.md(read_file) -> resources",
        "messages": len(msgs),
        **s,
        "brief_chars": len(final),
        "log_file": _rel(log_path),
    }
    stat_path = os.path.join(OUT_DIR, f"stats_deepagents_{run_id}.json")
    with open(stat_path, "w", encoding="utf-8") as f:
        json.dump(stat, f, ensure_ascii=False, indent=2)

    emit([f"简报已保存: {_rel(brief_path)}",
          f"统计已保存: {_rel(stat_path)}",
          f"日志已保存: {_rel(log_path)}"])
    log_fh.close()

    print("\n===== 最终简报 =====\n")
    print(final)


if __name__ == "__main__":
    main()