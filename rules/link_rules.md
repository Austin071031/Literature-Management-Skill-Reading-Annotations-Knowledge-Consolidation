# 关联判定规则（annotation-knowledge-sink）

本文件是 `scripts/pipeline.py` 中 `build_links` 的判定规则说明，供复用、审计与改进。所有关联必须带
`rationale`（可解释的关联依据），不接受“仅共享一个常见术语就连成一片”的过度连接。

## 一、笔记级关联（notes ↔ notes）

前提：从笔记的引文 + 批注文字中抽取实体，类型包括 `doi`、`arxiv`、`url`、`citation`（作者, 年）、
`quantity`（300 K、5 GPa 等）、`method`（内置 60+ 术语词表，如 density functional theory、
active learning、graph neural network）。

| 类型 | 判定条件 | 强度 | 依据 |
|-|-|-|-|
| `same_reference` | 共享 DOI 或 arXiv 实体 | 1.0 | 同一文献标识符，最强关联 |
| `related` | 共享 ≥1 个**稀有实体**（文档频率 df ≤ 3）或 ≥2 个任意实体 | min(1, 0.4 + 0.2·n_shared) | rationale 列出全部共享实体 |
| `co_mention` | 同文件内共享实体（不满足以上） | 0.3 | 同文献共现，弱关联 |
| （不建边） | 仅共享 1 个普通实体且跨文件 | — | 视为关键词重合噪声，控制过度连接 |

剪枝：每个笔记节点最多保留 12 条最强关联，其余丢弃并在 run.log 记录丢弃数量。

## 二、文件级关联（files ↔ files）

| 类型 | 判定条件 | 依据 |
|-|-|
| `same_content_duplicate` | sha256 完全相同 | 内容寻址，确定性判定 |
| `version_of` | 文件名规范化后相同、内容不同 | 规范化 = 去扩展名、小写、去除版本 token（v1、2024-01-02、draft/final/rev、副本/打印/扫描等）后按 mtime 排序 |

## 三、跨版本批注归并（clusters）

同一版本组内、规范化引文（小写、压缩空白）完全相同的批注聚为一个 `cluster_id`（C###），
用于解决“同一文献重复下载、多版本并存把同一批注拆散”的问题。归并不跨不同版本组，
避免把不同文献的巧合引文错误合并。

## 四、检索入口与召回处理

- 全文检索：SQLite FTS5（unicode61），中文查询自动切二元组并回退 LIKE 打分排序。
- 召回不足（命中 < topk/2 或无命中）：报告中自动标注，建议换关键词/去虚词/检查 pipeline 状态。
- 误召回处理：置信度 < 0.5 且锚点未验证的命中自动加 ⚠ 标注，提示引用前核对原文。

## 五、已知边界

- 版本判定不计算全文相似度，改名彻底的版本可能漏分组（记录在 uncertainty）。
- `method` 词表面向计算科学/材料领域，其他学科可按同格式扩充（pipeline.py 中 METHOD_LEXICON）。
- `citation` 实体基于 “(Author, Year)” 形态正则，不解析参考文献列表结构。
