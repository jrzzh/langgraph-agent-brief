已完成检索与核验。按 skills 要求（search-strategy 中英双语关键词、source-triage 分级、fact-cross-verify 逐条标注、brief-writing 结构、citation-format 去重/截断），输出简报如下。

---

# 大模型上下文工程 · 技术简报

**结论**：上下文工程是提示工程之后的系统性范式，核心是决定「何时、以何种形式、把什么信息放进有限的上下文窗口」，已成为构建可靠 Agent 的关键。

**已核验事实**
1. 2025 年 9 月 Anthropic 发文，将上下文工程定义为「推理期间维护并优化 token 集合」，视其为提示工程的演进。— 多来源一致（官方＋多个二手）
2. 上下文窗口可类比「短期记忆／工作内存」，容纳系统提示、对话历史、工具与检索内容，超限会报错。— 多来源一致（Google、Palantir 官方文档）
3. Google 官方文档称 Gemini 是首个支持 100 万 token 上下文的模型；其 1M 窗口在输入、思考与输出间共享。— 单一来源（Google 自述）
4. Stanford 2023 年论文《Lost in the Middle》发现：相关信息在上下文中间时模型表现最差，性能呈 U 形曲线。— 多来源一致（论文＋多个二手）
5. IBM 苏黎世论文：为 GPT-4.1 配「认知工具」后，其 AIME2024 的 pass@1 由 26.7% 升至 43.3%，接近 o1-preview。— 多来源一致（论文摘要＋多个二手）

**结论与限制**：核心事实以官方文档、论文与一手引用为锚；「Gemini 首个 1M」为厂商自述；「某 CEO 2025 年首提该词」等说法因缺一手核验未纳入。

**来源**
- [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Long context | Gemini API](https://ai.google.dev/gemini-api/docs/long-context)
- [Chatbot Studio 核心概念 — Palantir](https://www.palantir.com/docs/zh/foundry/chatbot-studio/core-concepts)
- [Lost in the Middle (arXiv:2307.03172)](https://arxiv.org/abs/2307.03172)
- [Eliciting Reasoning in Language Models…（BAAI）](https://hub.baai.ac.cn/paper/b8bf5d19-d208-4042-9223-b4a833ae1bcf)