已完成检索、来源分级与交叉核验。以下为最终简报。

---

# 技术简报：大模型上下文工程

**一句话结论**：上下文工程是提示词工程的演进方向，指在推理期间动态筛选、维护进入有限上下文窗口的最优信息，已成为智能体开发的核心工程实践。

**已核验事实**：

1. 它是提示词工程的自然演进，指在 LLM 推理期间筛选与维护最优 token 集合的策略。【多来源一致】
2. 方法论可归纳为四个动作：写、选、压、隔（write/select/compress/isolate）。【多来源一致】
3. 提示词工程是"一次写好"的离散任务，上下文工程则是迭代过程，每次调用前都需重新整理上下文。【多来源一致】
4. Anthropic 应用 AI 团队于 2025 年 9 月 29 日发文系统阐述该范式。【多来源一致】
5. 上下文指采样时输入的 token 集合，受窗口长度限制；上下文工程即从不断演化的信息中挑选进入有限窗口的内容。【多来源一致】

**结论与限制**：上述定义与框架以 Anthropic 官方文章为锚点，并与 Snowflake、百科等二手来源交叉一致；四动作框架在部分中文文章中有不同译法或扩展。「上下文工程」仍属演进中的术语，暂无统一行业标准。

**来源**（不计入字数）：

- [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [上下文工程（百度百科）](https://baike.baidu.com/item/上下文工程/67621569)
- [Context Engineering for AI Systems（Snowflake）](https://www.snowflake.com/en/artificial-intelligence/ai-engineering/context-engineering/)
- [Advanced Prompt Engineering for All（Coursera）](https://www.coursera.org/learn/advanced-prompt-engineering-for-all)
- [构建AI智能体之路：高效的上下文工程（掘金）](https://juejin.cn/post/7559451160634064931)

---

**核验说明**（不计入正文字数）：A 级一手来源为 Anthropic 官方工程博客（已读正文）；百度百科正文抓取失败，仅依据其搜索摘要中的「自然演进」「写、选、压、隔」表述参与交叉核验，未声称阅读其正文。发布日期「2025 年 9 月 29 日」由掘金与 CSDN 两处一致表述，另有多处「2025 年 9 月」佐证。