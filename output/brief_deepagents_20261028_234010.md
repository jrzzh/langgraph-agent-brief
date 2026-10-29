已完成检索与核验。核验过程：Anthropic 官方页面为 A 级一手来源（已读正文）；术语起源经 InfoQ、Bilibili、今日头条等多篇二手报道一致转述；IBM 数值经 GitHub README 直接引用 IBM Zurich 原文、InfoQ 与多篇 CSDN 交叉印证，但论文无 arXiv 全文，标注局限。

以下是简报正文：

---

# 大模型上下文工程 技术简报

**结论**：上下文工程（Context Engineering）指在推理时整理并维护进入有限上下文窗口的最优 token 集合，正取代提示工程成为大模型应用的核心范式，是提示工程的自然演进。

**已核验事实**：
1. Anthropic 定义上下文工程为「推理时整理并维护最优 token 集」，是提示工程的演进。—— 多来源一致
2. 2025 年 Shopify CEO Lütke 提议以「上下文工程」取代「提示工程」，Karpathy 转推支持。—— 多来源一致（二手转述）
3. IBM 给 GPT-4.1 注入「认知工具」，AIME2024 准确率由 26.7% 升至 43.3%，逼近 o1-preview（44.6%）。—— 多来源一致
4. 上下文窗口是有限「工作内存」，需在每次调用前迭代筛选进入窗口的信息。—— 多来源一致

**结论与限制**：核心是每次调用前动态筛选、压缩、检索上下文，而非雕琢单条提示。限制：IBM 数值未读论文全文，仅经 InfoQ 摘要与 GitHub 引文核对；术语起源为二手报道，未核原推文。

**来源**：
- [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [一文说明白 Context Engineering：AI 智能体的动态语境构建术](https://xie.infoq.cn/article/611f5e8b431f5c9d54073b6c7)
- [Context-Engineering](https://github.com/davidkimai/Context-Engineering)

---

说明：正文约 400 字（≤500 字上限），「来源」小节未计入字数。术语起源与 IBM 数值均属二手转述，未达到一手原文核验标准，已在「结论与限制」中如实标注。