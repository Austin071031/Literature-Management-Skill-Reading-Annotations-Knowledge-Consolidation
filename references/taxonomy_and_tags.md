# 文献类型判定与标签规范

## 1. 文献类型判定规则表（自上而下，命中即停）

| 规则 | 类型 | 证据 |
|-|-|-|
| T0 | other_document | 纯元数据侧车（.bib/.ris/.enw） |
| T1 | dataset | 魔数为 zip/gzip/rar，或扩展名为 csv/xlsx/xls/json/cif/xyz |
| T2 | supplement | 文件名含 suppl/supporting/_si/appendix/补充 |
| T3 | supplement | 文件名提示命中（补充材料/附录） |
| T4 | scanned_document | PDF 无文本层（前两页文本字符数为 0）且未加密、无侧车 |
| T5 | thesis | thesis/dissertation/学位 字样（文本或文件名） |
| T6 | preprint | 有 arXiv 号且无期刊+DOI 组合元数据 |
| T7 | preprint | 文本含 preprint/arxiv/biorxiv/预印本 字样 |
| T8 | conference_paper | 期刊名含 proc./conference/symposium/workshop，或侧车为 inproceedings |
| T9 | journal_article | 有 DOI 或期刊名或侧车为 article |
| T10 | journal_article | 兜底：PDF 有文本层（低置信，需在 uncertainty 说明） |
| T11 | other_document | 无任何文献特征 |

每条判定在 manifest 的 `reason` 与 `classify_rule.rule_id` 中留痕，可逐条复核。

## 2. 版本关系（并查集分组）

分组键（满足其一即并入同组）：
1. **DOI**（规范化：去 `https://doi.org/` 前缀、小写、去尾部标点）；
2. **规范化标题 + 第一作者姓**（标题去标点/空白/大小写后 ≥12 字符）。

同组文件判定为同一工作的不同版本（预印本↔正式版、v1↔v2 等）。
canonical 选择：按 (有DOI+4, 有期刊名+2, 有年份+1, 有标题+1, 有文本层+1) 打分，
分数最高者为正式保留版，其余归档 `_archive/versions/<组键>/`（**归档不删除**）。
侧车文件不参与版本分组。

## 3. 精确重复（同 sha256）

保留文件名最干净的一份（依次惩罚：含 `(数字)`、含 download/副本/copy、空格数、长度），
其余归档 `_archive/duplicates/`。保留判定纯按文件名可辨识性，不引入主观猜测。

## 4. 受控标签（避免标签爆炸）

标签只从**原文 Keywords 行 + 标题**中命中生成，两个来源：
1. **受控词表**（plan.py `TAG_VOCAB`，同义词合并到规范形，如 dft/Density Functional/密度泛函
   → `density-functional-theory`）；
2. **原文关键词直出**（受控表外，最多 3 个，规范化为小写连字符形）。

总量上限 `max_tags`（默认 6）。标签命中情况逐文件见 manifest 的 `tags` 列；
未命中任何词的文献标签为空列表——**不从全文臆推标签**。

## 5. 跨主题与边界模糊文献

- 目录归属是单层的（每文件一个 doc_type + 一个年份目录）；跨主题信息由**多标签**承载，
  不复制文件到多个目录（避免制造伪重复）。
- T10 兜底判定（仅有文本层、无其它特征）置信度低，统一在 result.json 的
  `uncertainty` 中点名，供人工复核。
