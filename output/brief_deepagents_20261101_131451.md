已完成检索与核验。以下是简报：

---

# 大模型上下文工程 技术简报

**结论**：上下文工程把重点从「怎么写提示词」移到「每次推理给模型看哪些 token」。

**已核验事实**

1. 定义：上下文是采样时纳入 LLM 的 token 集合；上下文工程是在推理中筛选并维护这组 token 的策略集合。**多来源一致**（Anthropic 官方、百度百科）
2. 定位：Anthropic 称其为提示词工程的自然演进；前者是离散的一次性写作，后者在每次调用前反复发生。**多来源一致**（Anthropic、中文技术报道）
3. 动因：上下文越长性能越差（context rot），简单任务亦然；Chroma 2025 报告称 18 个前沿模型同向退化。**多来源一致**；「18 个模型」仅二手转述，原始报告未读。**待核实**
4. 手法：压缩（接近上限时摘要并重开窗口）、结构化笔记（状态写到窗口外，如 NOTES.md）、子智能体隔离噪声。**多来源一致**（二手描述一致；官方正文该节未取到）
5. 由来：2025 年 6 月 Tobi Lütke 提倡该词、Karpathy 附议；7 月 17 日综述 arXiv 2507.13334 发布。**多来源一致**；另说 6 月 25 日。**待核实**

**限制**：arXiv 与百科正文抓取失败，部分细节仅据检索摘要，未视为已读。

**来源**

- [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [上下文工程（百度百科）](https://baike.baidu.com/item/%E4%B8%8A%E4%B8%8B%E6%96%87%E5%B7%A5%E7%A8%8B/67621569)
- [从 Prompt Engineering 到 Context Engineering](https://blog.csdn.net/2301_80370251/article/details/164118136)
- [Awesome Context Engineering（综述官方仓库）](https://github.com/Meirtz/Awesome-Context-Engineering)

---

**执行说明（不计入字数）**

- 检索按技能要求走中英双语、2–5 词关键词：「context engineering LLM」「上下文工程 大模型」「context engineering survey arxiv」「context rot long context performance degradation」，首轮即命中 A 级来源，无需改写。
- 来源分级：Anthropic 工程博客 = A（一手）；arXiv 2507.13334 经其官方 GitHub 仓库 README 交叉确认（2025.07.17 发布）；百度百科 = B；CSDN/聚合站 = C，仅用于转述且已标注。
- 抓取失败情况：`arxiv.org/abs/2507.13334`、`export.arxiv.org`、百度百科正文均返回空，故第 3、5 条中依赖正文的细节按「仅据摘要」处理，未宣称读过原文。
- 未纳入：某聚合站「综述覆盖 1400+ 篇文献」的说法，仅单一 C 级来源，且无法回溯原文，按技能要求舍弃。
- 正文字数约 450 字（去 Markdown 语法与空白后估算），「来源」小节未计入。