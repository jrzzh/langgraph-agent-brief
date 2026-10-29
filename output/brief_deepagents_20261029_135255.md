# 大模型上下文工程 · 技术简报

上下文工程已成为提示工程之后构建可靠 LLM/Agent 应用的核心方法，核心命题从「如何写提示」转向「每次推理前给模型哪些信息」。

**已核验事实**

1. 定义：上下文是 LLM 采样时包含的 token 集合；上下文工程是优化其效用、以稳定达成结果的策略集合。多来源一致（Anthropic 原文＋教程）。
2. 与提示工程的关系：上下文工程是提示工程的演进；提示工程离散、一次写好，上下文工程迭代、每次调用前筛选。多来源一致（Anthropic 原文＋教程复述）。
3. 上下文窗口：LLM 单次可处理的 token 量，含系统提示、对话历史与注入信息，超限报错。多来源一致（Palantir、Anthropic 官方文档）。
4. 窗口被教程比作工作内存；信息过多、重复或矛盾会让模型漏掉关键条件。单一来源（二手教程）。
5. 常用手段：上下文选择、压缩、摘要、检索增强（RAG）、记忆与避免过载。多来源一致（官方 RAG 文档＋教程）。

**结论与限制**：以上事实由官方一手来源锚定并经二手来源交叉验证；上下文窗口长度演进（4K→128K/百万 token）等数值口径不一、未纳入，属待核实。

**来源**

- [Effective context engineering for AI age…](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [大模型上下文工程：定义、价值与Agent开发实践](https://developer.baidu.com/article/detail.html?id=7813467)
- [小白程序员必备：手把手教你驾驭大模型完成复杂任务](https://blog.csdn.net/l01011_/article/details/168962667)
- [Palantir Chatbot Studio 核心概念](https://www.palantir.com/docs/zh/foundry/chatbot-studio/core-concepts)
- [掌握大模型上下文管理：小白程序员必看，附带收藏指南](https://blog.csdn.net/2401_85378759/article/details/166137305)