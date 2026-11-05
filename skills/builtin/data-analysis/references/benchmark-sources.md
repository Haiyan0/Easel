# 对标来源与适配记录

核查日期：2026-10-08。采用官方仓库中的技能正文，结合公开采用信号挑选参考；安装数与仓库星数不能证明分析准确性，也没有足以支持“市面第一”的统一排名。

| 来源 | 借鉴的方法 | 本项目适配 |
| --- | --- | --- |
| [Anthropic explore-data](https://github.com/anthropics/knowledge-work-plugins/blob/main/data/skills/explore-data/SKILL.md) | 粒度、键、类型、缺失和范围探索 | 用户提供的本地文件，避免把抽样称为全量 |
| [Anthropic validate-data](https://github.com/anthropics/knowledge-work-plugins/blob/main/data/skills/validate-data/SKILL.md) | 分母、连接、独立核验、交付判断 | 保存实际检查结果，不只勾清单 |
| [Anthropic data-visualization](https://github.com/anthropics/knowledge-work-plugins/blob/main/data/skills/data-visualization/SKILL.md) | 按沟通目的选图、轴和单位 | 中文 PNG 与离线 Plotly，按需要输出 |
| [Anthropic statistical-analysis](https://github.com/anthropics/knowledge-work-plugins/blob/main/data/skills/statistical-analysis/SKILL.md) | 分布、异常、结构影响 | 描述与因果分开，不虚构推断能力 |
| [Anthropic xlsx](https://github.com/anthropics/skills/blob/main/skills/xlsx/SKILL.md) | 公式与数值缓存可靠性 | 双次读取审计；当前无重算引擎 |
| [K-Dense exploratory-data-analysis](https://github.com/K-Dense-AI/scientific-agent-skills/blob/main/skills/exploratory-data-analysis/SKILL.md) | 缺失与零、检查范围、异常敏感性 | 不引入整套科学文件格式及依赖 |
| [K-Dense statistical-analysis](https://github.com/K-Dense-AI/scientific-agent-skills/blob/main/skills/statistical-analysis/SKILL.md) | 独立单位、效应与不确定性、探索边界 | 仅在任务与现有能力允许时开展推断 |
| [Claude Office data-analysis](https://github.com/claude-office-skills/skills/blob/main/data-analysis/SKILL.md) | 摘要、概览、报告组织 | 使用本项目已存在的文件与本地 Python |

本文及其他参考文档为原创整理和项目适配，不逐字复制外部技能或引入其工具声明。尤其不把 xlsx 技能的专有许可正文当作可自由复制的模板。公开正文可能持续更新，此记录对应上述核查日期。

保留原有两档模式、数据读取纪律、图表交付和文件交接机制；不接入外部数据库，不增加运行依赖。行为评测是有限场景验证，不代表通用准确率或实际线上模型的保证。
