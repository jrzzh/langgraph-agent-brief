# -*- coding: utf-8 -*-
"""
Streamlit 实测台：同一任务、两种 Skills 实现可选
================================================================
一次运行 = 联网检索 → 关键信息提取 → 事实交叉核验 → 500 字以内带来源链接的中文技术简报。

界面提供两项关键选择：
1. **Skills 实现方式**
   - ``skill_loader``：自建技能层（固定 6 节点流水线，lazy/eager/off 三模式）
   - ``deepagents``  ：官方原生 Skills（模型自主循环，三级渐进式披露）
2. **技能加载模式**（仅自建实现可调）：lazy（默认）/ eager（对照）/ off（基线）

运行后可查看：指标卡、技术简报（可下载）、完整运行日志、统计 JSON。

启动（项目由 uv 管理）：
    uv run streamlit run streamlit_app.py
或双击 ``start-ui.cmd``。
"""

from __future__ import annotations

import glob
import json
import os

import streamlit as st

import agent_brief as ab

st.set_page_config(page_title="LangGraph 单框架实测台", page_icon="🕸️", layout="wide")

PRESET_TOPICS = [
    "大模型上下文工程",
    "MCP 的基本架构与适用场景",
    "RAG 与长上下文的关系",
    "Agent 记忆机制",
    "自定义主题",
]
IMPLS = {
    "skill_loader": "自建 skill_loader（固定 6 节点流水线）",
    "deepagents": "官方 deepagents（模型自主循环）",
}
SKILL_MODES = {
    "lazy": "lazy · 只注入目录，正文按需加载",
    "eager": "eager · 启动时全量注入正文",
    "off": "off · 不注入技能（基线）",
}


# ---------------------------------------------------------------------------
# 读取与归一
# ---------------------------------------------------------------------------
def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _skills_loaded_count(s: dict) -> tuple[int, int]:
    """返回 (已加载技能数, 可用技能数)，兼容两种实现的统计字段。"""
    skills = s.get("skills") or {}
    if skills:
        return int(skills.get("loaded_count", 0)), int(skills.get("skill_count", 0))
    loaded = s.get("skills_loaded") or []
    available = s.get("skills_available") or []
    return len(loaded), len(available)


def _tool_call_count(s: dict) -> int:
    calls = s.get("tool_calls")
    if isinstance(calls, int):
        return calls
    if isinstance(calls, dict):
        return sum(int(v) for v in calls.values())
    return 0


def _elapsed(s: dict) -> float:
    return float(s.get("total_elapsed_s") or s.get("wall_elapsed_s") or 0)


def _impl_label(s: dict) -> str:
    impl = s.get("impl") or ""
    return "deepagents" if impl.startswith("deepagents") else "skill_loader"


# ---------------------------------------------------------------------------
# 历史运行
# ---------------------------------------------------------------------------
def _history() -> list[dict]:
    items: list[dict] = []
    for pattern in (
        os.path.join(ab.OUT_DIR, "stats_*.json"),
        os.path.join(ab.OUT_DIR, "stats_deepagents_*.json"),
    ):
        for path in glob.glob(pattern):
            name = os.path.basename(path)
            if name.startswith("stats_deepagents_"):
                run_id = name[len("stats_deepagents_"):-len(".json")]
            else:
                run_id = name[len("stats_"):-len(".json")]
            stats = _load_json(path)
            if stats:
                items.append({"run_id": run_id, "stats": stats})
    seen: set[str] = set()
    unique = []
    for item in sorted(items, key=lambda x: x["run_id"], reverse=True):
        if item["run_id"] not in seen:
            seen.add(item["run_id"])
            unique.append(item)
    return unique


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _render_run(summary: dict, *, key_prefix: str = "latest") -> None:
    stats = summary.get("stats") or summary
    run_id = stats.get("run_id", "?")
    impl = _impl_label(stats)

    st.subheader(stats.get("topic") or "（未命名主题）")
    st.caption(
        f"RUN_ID `{run_id}` ｜ 实现 `{impl}` ｜ "
        f"模型 `{stats.get('model') or os.environ.get('DEEPSEEK_MODEL', '-')}`"
    )

    loaded, available = _skills_loaded_count(stats)
    metrics = st.columns(5)
    metrics[0].metric("总耗时", f"{_elapsed(stats)} s")
    metrics[1].metric(
        "总 token",
        f"{int(stats.get('token_input') or 0) + int(stats.get('token_output') or 0):,}",
    )
    metrics[2].metric("LLM 调用", stats.get("llm_calls", 0))
    metrics[3].metric("工具调用", _tool_call_count(stats))
    metrics[4].metric("技能加载", f"{loaded}/{available}" if available else "-")

    skills_field = stats.get("skills") or {}
    if skills_field:
        st.caption(
            f"技能层：mode=`{skills_field.get('mode')}` ｜ "
            f"目录 {skills_field.get('catalog_tokens', 0)} token ｜ "
            f"注入正文 {skills_field.get('loaded_body_tokens', 0)} token ｜ "
            f"扫描 {skills_field.get('scan_ms', 0)} ms"
        )
    if stats.get("skills_source"):
        st.caption(f"技能源：`{stats['skills_source']}` ｜ {stats.get('skills_disclosure', '')}")
    if stats.get("skills_used"):
        st.caption("路由选中的技能：" + "、".join(stats["skills_used"]))
    elif stats.get("skills_loaded"):
        st.caption("本次读取的技能：" + "、".join(stats["skills_loaded"]))

    brief_tab, log_tab, stat_tab, err_tab = st.tabs(
        ["📄 技术简报", "🪵 运行日志", "📊 统计 JSON", "⚠️ 故障记录"]
    )

    with brief_tab:
        brief = summary.get("brief")
        if brief is None:
            filename = (f"brief_deepagents_{run_id}.md" if impl == "deepagents"
                        else f"brief_{run_id}.md")
            brief = _read_text(os.path.join(ab.OUT_DIR, filename))
        if brief and brief.strip():
            st.markdown(brief, unsafe_allow_html=False)
            st.download_button(
                "下载 Markdown 简报",
                data=brief,
                file_name=f"brief_{run_id}.md",
                mime="text/markdown",
                key=f"{key_prefix}-dl-{run_id}",
            )
        else:
            st.info("该次运行没有生成简报。")

    with log_tab:
        log_text = summary.get("log_text")
        if not log_text:
            rel = stats.get("log_file") or summary.get("log_path")
            if not rel:
                rel = (f"logs/deepagents_{run_id}.log" if impl == "deepagents"
                       else f"logs/run_{run_id}.log")
            log_text = _read_text(os.path.join(ab.BASE_DIR, rel))
            st.caption(f"日志文件：`{rel}`")
        st.code(log_text or "（日志为空或已被清理）", language="text")

    with stat_tab:
        st.json(stats, expanded=False)

    with err_tab:
        errors = stats.get("errors") or []
        if errors:
            for item in errors:
                st.warning(str(item))
        else:
            st.success("本次运行无故障记录。")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def _run_tab() -> None:
    st.caption(
        "统一任务：联网检索 → 关键信息提取 → 事实交叉核验 → 生成 500 字以内带来源链接的中文技术简报"
        "（500 字仅约束正文，来源不计入）"
    )

    left, right = st.columns(2)
    with left:
        preset = st.selectbox("业务主题", PRESET_TOPICS, index=0)
        topic = (
            st.text_input("自定义主题", value="", placeholder="例如：向量数据库选型要点")
            if preset == "自定义主题"
            else preset
        )
    with right:
        impl = st.selectbox(
            "Skills 实现方式",
            list(IMPLS),
            index=0,
            format_func=lambda k: IMPLS[k],
            help="自建实现走固定 6 节点流水线，可调技能加载模式；"
                 "deepagents 由模型自主决定是否读取技能。",
        )
        is_deep = impl == "deepagents"
        skill_mode = st.selectbox(
            "技能加载模式",
            list(SKILL_MODES),
            index=0,
            format_func=lambda k: SKILL_MODES[k],
            disabled=is_deep,
            help="仅自建 skill_loader 实现可调；deepagents 的加载时机由框架决定。",
        )
        if is_deep:
            st.caption("deepagents 的技能加载由框架内部决定，此选项不生效。")

    st.divider()
    if st.button("▶ 运行统一任务", type="primary"):
        if not topic.strip():
            st.error("请填写主题。")
            return
        if not os.environ.get("DEEPSEEK_API_KEY"):
            st.error("未检测到 DEEPSEEK_API_KEY，无法调用模型。")
            return
        label = f"运行中……（实现 {impl}" + ("" if is_deep else f" / 技能 {skill_mode}") + "）"
        with st.spinner(label):
            try:
                if is_deep:
                    import agent_deepagents as dd

                    summary = dd.run_task(topic.strip())
                else:
                    summary = ab.run_task(topic.strip(), skill_mode)
            except Exception as exc:                       # noqa: BLE001 - 界面需显示原因
                st.error(f"运行失败：{type(exc).__name__}: {exc}")
                return
        st.session_state["last_summary"] = summary
        st.success(f"完成：RUN_ID `{summary.get('run_id')}`")

    last = st.session_state.get("last_summary")
    if last:
        st.divider()
        _render_run(last, key_prefix="latest")
    else:
        st.info("选择主题与 Skills 实现方式后，点击「运行统一任务」。")


def _history_tab() -> None:
    items = _history()
    if not items:
        st.info("还没有历史运行。先在上方运行一次，或用命令行执行。")
        return
    labels = [
        f"{it['run_id']} · {_impl_label(it['stats'])} · "
        f"{it['stats'].get('topic', '')} · {_elapsed(it['stats'])}s"
        for it in items
    ]
    choice = st.selectbox("选择历史运行", range(len(items)), format_func=lambda i: labels[i])
    _render_run({"stats": items[choice]["stats"]}, key_prefix="history")


def _skills_catalog() -> list[dict]:
    """扫描技能目录，供侧栏展示。"""
    if not ab.SKILLS_AVAILABLE:
        return []
    try:
        registry = ab.SkillRegistry(mode="lazy").scan()
    except Exception:
        return []
    return [
        {
            "技能": meta.name,
            "版本": meta.version,
            "正文(字符)": meta.body_chars,
            "正文(token)": meta.body_tokens,
            "描述": (meta.description or "")[:36],
        }
        for meta in registry.skills.values()
    ]


def main() -> None:
    st.title("LangGraph 单框架实测台")
    st.caption("同一任务 × 两种 Skills 实现（自建 skill_loader / 官方 deepagents）× 可复核的运行日志")

    with st.sidebar:
        st.header("环境")
        st.write(f"模型：`{os.environ.get('DEEPSEEK_MODEL', '未设置')}`")
        st.write(f"接口：`{os.environ.get('DEEPSEEK_BASE_URL', '-')}`")
        if os.environ.get("DEEPSEEK_API_KEY"):
            st.success("DEEPSEEK_API_KEY 已配置")
        else:
            st.warning("未检测到 DEEPSEEK_API_KEY")
        st.caption("密钥只从启动终端的环境变量 / .env 读取，浏览器端不填写。")
        st.divider()
        st.header("两种实现")
        st.write("**① skill_loader**：固定 6 节点，技能由一次受约束的路由调用选择")
        st.write("**② deepagents**：模型自主循环，自行 read_file 读取 SKILL.md")
        st.divider()
        st.header("技能目录")
        catalog = _skills_catalog()
        if catalog:
            st.dataframe(catalog, hide_index=True)
        else:
            st.info("skills/ 下没有可用技能。")

    tabs = st.tabs(["运行实验", "历史记录"])
    with tabs[0]:
        _run_tab()
    with tabs[1]:
        _history_tab()


if __name__ == "__main__":
    main()