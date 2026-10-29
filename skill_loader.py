# -*- coding: utf-8 -*-
"""
Skills 运行时：声明式技能单元 + 渐进式披露（progressive disclosure）
================================================================
对应课题研究问题与目标：
  Q1「各框架如何封装可复用技能？」        → 用「声明式 SKILL.md + 运行时注册表」封装
  Q3「加载时机：启动全量 vs 运行时按需？」 → 本模块实现 lazy / eager 两模式**并全程计量**
  维度 2「Skills 封装方式 / 复用粒度 / 新增技能工作量」
  维度 4「工具加载时机、大量工具下内存与启动耗时表现」

设计要点
--------
1. 技能 = 一个目录 + 一个 `SKILL.md`（frontmatter 写元信息，正文即注入用提示词）
2. **元信息常驻**上下文（name / description / tags），供路由器判断该不该用
3. **正文按需加载**：只有被选中的技能才把正文读入并注入 prompt —— 即渐进式披露
4. 全程计量：目录 token、正文 token、扫描耗时、预加载耗时，直接产出维度 4 的实测数据

用法
----
    reg = SkillRegistry(mode="lazy").scan()
    reg.load("search-strategy")          # 按需加载正文
    reg.catalog_text()                    # 注入给路由器的「技能目录」（仅元信息）
    reg.instructions()                    # 注入给执行节点的「技能正文」
    reg.stats()                           # 计量结果，写入 output/stats_*.json
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

# 项目根目录（skills/ 的上级），用于放置运行期缓存
PROJECT_DIR = os.path.dirname(SKILLS_DIR)

# frontmatter：以 --- 包裹的头部 + 其余正文
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# token 估算器：tiktoken 首次使用需下载词表（约 1.7MB），故缓存编码器并**单独计量其初始化耗时**，
# 避免污染「技能扫描」的计时。离线环境可设 SKILL_TOKEN_ESTIMATOR=heuristic 走启发式，免网络依赖。
_ENCODER = None
_ENCODER_READY = False
_ENCODER_INIT_MS = 0.0
_ENCODER_NAME = ""


def get_encoder():
    """惰性初始化并缓存 token 编码器，返回 (encoder 或 None, 估算器名称)。"""
    global _ENCODER, _ENCODER_READY, _ENCODER_INIT_MS, _ENCODER_NAME
    if _ENCODER_READY:
        return _ENCODER, _ENCODER_NAME
    _ENCODER_READY = True
    if os.environ.get("SKILL_TOKEN_ESTIMATOR", "").strip().lower() == "heuristic":
        _ENCODER, _ENCODER_NAME = None, "heuristic(cjk*0.7+other/4)"
        return _ENCODER, _ENCODER_NAME
    # tiktoken 默认缓存到 tempfile.gettempdir()；若 TEMP/TMP 不可用（如某些 IDE 运行环境），
    # Python 会退回到当前工作目录，在仓库根生成 1.6MB 的 data-gym-cache/。
    # 这里显式固定到项目内 .cache/tiktoken（已在 .gitignore 忽略），保证位置可预期。
    if not os.environ.get("TIKTOKEN_CACHE_DIR"):
        _cache_dir = os.path.join(PROJECT_DIR, ".cache", "tiktoken")
        try:
            os.makedirs(_cache_dir, exist_ok=True)
            os.environ["TIKTOKEN_CACHE_DIR"] = _cache_dir
        except OSError:
            pass  # 目录不可写时退回 tiktoken 默认行为，不影响功能
    t0 = time.time()
    try:
        import tiktoken

        _ENCODER = tiktoken.get_encoding("cl100k_base")
        _ENCODER_NAME = "tiktoken/cl100k_base"
    except Exception:
        _ENCODER, _ENCODER_NAME = None, "heuristic(cjk*0.7+other/4)"
    _ENCODER_INIT_MS = round((time.time() - t0) * 1000, 2)
    return _ENCODER, _ENCODER_NAME


def estimate_tokens(text: str) -> tuple[int, str]:
    """估算 token 数，返回 (tokens, 估算器名称) —— 口径写入统计，保证结论可追溯。"""
    if not text:
        return 0, _ENCODER_NAME or "empty"
    enc, name = get_encoder()
    if enc is not None:
        return len(enc.encode(text)), name
    cjk = len(_CJK_RE.findall(text))
    return int(cjk * 0.7 + (len(text) - cjk) / 4), name


def parse_skill_file(path: str) -> tuple[dict, str]:
    """解析 SKILL.md，返回 (元信息 dict, 正文 str)。

    自行解析 frontmatter 的简单 `key: value` / `key: [a, b]` 形式，
    不引入 yaml 依赖，保证公开仓库在任何环境都能跑。
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    m = FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw.strip()
    meta: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k] = v.strip("'\"")
    return meta, m.group(2).strip()


@dataclass
class SkillMeta:
    """一个技能的元信息 + 加载状态。"""

    name: str
    description: str = ""
    version: str = "1.0"
    tags: list = field(default_factory=list)
    priority: str = "normal"
    path: str = ""
    body_chars: int = 0
    body_tokens: int = 0
    body: str = ""            # 仅按需加载后才有值
    loaded: bool = False
    load_ms: float = 0.0


class SkillRegistry:
    """技能注册表：扫描元信息 → 路由 → 按需加载正文。"""

    def __init__(self, skills_dir: str = SKILLS_DIR, mode: str = "lazy"):
        self.skills_dir = skills_dir
        self.mode = mode if mode in ("lazy", "eager", "off") else "lazy"
        self.skills: dict = {}
        self.scan_ms: float = 0.0
        self.preload_ms: float = 0.0
        self.estimated_by: str = "empty"

    # ---------------- 扫描：只读元信息，正文仅计量不驻留 ----------------
    def scan(self) -> "SkillRegistry":
        get_encoder()          # 先初始化编码器，使其耗时不计入「技能扫描」
        t0 = time.time()
        if self.mode != "off" and os.path.isdir(self.skills_dir):
            for entry in sorted(os.listdir(self.skills_dir)):
                d = os.path.join(self.skills_dir, entry)
                f = os.path.join(d, "SKILL.md")
                if not (os.path.isdir(d) and os.path.isfile(f)):
                    continue
                try:
                    meta, body = parse_skill_file(f)
                except Exception:
                    continue
                name = meta.get("name") or entry
                tokens, est = estimate_tokens(body)
                self.estimated_by = est
                self.skills[name] = SkillMeta(
                    name=name,
                    description=meta.get("description", ""),
                    version=str(meta.get("version", "1.0")),
                    tags=meta.get("tags", []) or [],
                    priority=str(meta.get("priority", "normal")),
                    path=f,
                    body_chars=len(body),
                    body_tokens=tokens,
                )
        self.scan_ms = round((time.time() - t0) * 1000, 2)
        return self

    # ---------------- 目录：常驻上下文的那一小块 ----------------
    def catalog_text(self) -> str:
        if not self.skills:
            return ""
        lines = ["【可用技能目录】仅元信息常驻；技能正文在选中后才加载。"]
        for s in self.skills.values():
            tag = f"（tags: {','.join(s.tags)}）" if s.tags else ""
            lines.append(f"- {s.name} v{s.version}{tag}：{s.description}")
        return "\n".join(lines)

    # ---------------- 按需加载正文 ----------------
    def load(self, name: str) -> SkillMeta | None:
        s = self.skills.get(name)
        if s is None or s.loaded:
            return s
        t0 = time.time()
        try:
            _, body = parse_skill_file(s.path)
        except Exception:
            body = ""
        s.body = body
        s.loaded = True
        s.load_ms = round((time.time() - t0) * 1000, 2)
        return s

    def load_all(self) -> "SkillRegistry":
        """eager 模式：启动时把全部技能正文读入（对照组）。"""
        t0 = time.time()
        for name in list(self.skills):
            self.load(name)
        self.preload_ms = round((time.time() - t0) * 1000, 2)
        return self

    # ---------------- 注入文本 ----------------
    def instructions(self, names: list | None = None) -> str:
        """拼装注入 prompt 的技能正文。

        names 为 None 时取「全部已加载」的技能（eager 模式即全量）。
        """
        if names:
            picked = [self.skills[n] for n in names if n in self.skills]
        else:
            picked = [s for s in self.skills.values() if s.loaded]
        blocks = [f"### 技能：{s.name}\n{s.body}" for s in picked if s.body]
        return "\n\n".join(blocks)

    # ---------------- 计量：维度 4 的填表数据来源 ----------------
    def stats(self) -> dict:
        loaded = [s.name for s in self.skills.values() if s.loaded]
        cat = self.catalog_text()
        cat_tokens, est = estimate_tokens(cat) if cat else (0, self.estimated_by)
        loaded_body_tokens = sum(s.body_tokens for s in self.skills.values() if s.loaded)
        return {
            "mode": self.mode,
            "skill_count": len(self.skills),
            "skill_names": list(self.skills.keys()),
            "catalog_chars": len(cat),
            "catalog_tokens": cat_tokens,
            "loaded": loaded,
            "loaded_count": len(loaded),
            "loaded_body_chars": sum(s.body_chars for s in self.skills.values() if s.loaded),
            "loaded_body_tokens": loaded_body_tokens,
            "all_body_chars": sum(s.body_chars for s in self.skills.values()),
            "all_body_tokens": sum(s.body_tokens for s in self.skills.values()),
            "scan_ms": self.scan_ms,
            "preload_ms": self.preload_ms,
            "loaded_detail": {s.name: {"chars": s.body_chars, "tokens": s.body_tokens,
                                       "load_ms": s.load_ms}
                              for s in self.skills.values() if s.loaded},
            "token_estimator": _ENCODER_NAME or est,
            "estimator_init_ms": _ENCODER_INIT_MS,
        }