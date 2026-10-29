核验完成。以下为技术简报。

---

# 大模型上下文工程·技术简报

**结论**：上下文工程是提示工程的延续，指在推理期间筛选并维护送入有限上下文窗口的最优 token 集合，已成为构建可靠智能体的核心手段。

**已核验事实**

1. Anthropic 将其界定为「在 LLM 推理期间筛选与维护最优 token 集合的策略集合」，视作提示工程的自然演进。——多来源一致
2. 与提示工程的「离散、一次性」不同，上下文工程是迭代的，每次决定向模型传递何种信息时都发生。——多来源一致
3. 上下文窗口是模型的「工作内存」，容量有限；内容过多、重复或矛盾会稀释有效信息，任务越长越依赖模型外编排软件。——多来源一致
4. Lost in the Middle（Liu 等，2023）：模型更关注上下文开头与结尾，中间信息易被忽略，性能呈 U 型曲线。——多来源一致
5. Context Rot（Chroma，2025-07）：输入 token 越多，模型准确回忆能力越差，衰减非均匀、呈「渐变而非断崖」，18 款主流模型皆如此。——多来源一致

**限制说明**：以上均为定义与机制性结论，有官方文档、论文或技术报告交叉印证；转载文中的营销性数值（如「压缩节省 60%」）未采信，本简报亦未做长上下文基准实测。

## 来源

- [Anthropic: Effective Context Engineering for AI Agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [百度开发者: 上下文工程核心技术解析](https://developer.baidu.com/article/detail.html?id=6340089)
- [Liu et al.: Lost in the Middle (arXiv)](https://arxiv.org/abs/2307.03172)
- [Chroma: Context Rot 技术报告](https://www.trychroma.com/research/context-rot)