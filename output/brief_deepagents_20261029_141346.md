已完成检索与交叉核验（先读取了 brief-writing、search-strategy、source-triage、fact-cross-verify、citation-format 五个技能并按方法执行）。以下为技术简报。

---

# 大模型上下文工程技术简报

**结论**：上下文工程是提示工程的延伸，重心从「怎么写提示词」转向「推理时如何策划并维护模型可见的最优信息集」。

**已核验事实**：

1. 定义：指推理期间策划并维护最优 token 集（信息）的策略，涵盖系统指令、工具、外部数据、消息历史等提示词之外的信息。——多来源一致（Anthropic 官方原文＋多二手转述）
2. 区别：提示工程关注单次调用的写法；上下文工程是迭代的，每次调用前决定给什么、如何组织、何时注入或撤出。——多来源一致（官方原文＋多二手对比）
3. 上下文窗口容量固定有限，系统提示、历史、工具说明、检索内容与输出都须容纳其中，常被类比为「工作内存」。——多来源一致
4. Lost in the Middle：长上下文中模型对开头、结尾利用更好，中间显著下降，呈 U 型曲线。——多来源一致（原论文＋多二手；期刊年份有口径差异）
5. Anthropic 于 2025 年 9 月发表系统阐述该范式的工程文章。——多来源一致（多二手一致；官方页面未直接显示日期）

**限制说明**：定义以 Anthropic 官方工程文章为锚点；「上下文工程」尚无统一行业标准；第 4 条论文期刊出版年份有 2023/2024 口径差异，以 arXiv 版本为准。

**来源**：
- [Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Lost in the Middle: How Language Models Use Long Contexts](https://arxiv.org/abs/2307.03172)
- [别再只卷 Prompt 了，真正拉开 Agent 差距的是 Context Engineering](https://view.inews.qq.com/wxn/20260914A0B89Z00?pushid=2020052660&reJumped=1)
- [上下文工程（Context Engineering）和提示工程](https://blog.csdn.net/usstmiracle/article/details/167274034)

---

**执行说明**：
- 检索采用中英双语、2–5 词关键词（「大模型 上下文工程」「context engineering LLM」等），按 source-triage 将 Anthropic 官方工程文章与 arXiv 论文定为 A 级锚点，二手博客（CSDN、腾讯新闻）仅作交叉验证。
- 关键事实均完成交叉核验并逐条标注证据强度；第 4、5 条的口径差异（期刊年份、发表日期）已在标注中显式说明，未做静默取舍。
- 正文约 460 字，「来源」小节不计入。