# 使用与方法说明（annotation-knowledge-sink v1.0.0）

## 0. 总览：两条流水线

本 Skill 包含两条独立可用、可合并交付的确定性流水线：

- **工作流 B：文档库整理** —— `scripts/run_pipeline.sh` 一键执行
  （inspect_case → inventory → extract_refs → plan → apply → report → validate_output）。
  输出布局、result.json 字段、manifest 列、异常处置详见 `../references/output_schema.md`、
  `../references/edge_cases.md`、`../references/naming_rules.md`、`../references/taxonomy_and_tags.md`、
  `../references/worked_example.md`。
- **工作流 A：批注与知识沉淀** —— `scripts/pipeline.py` + `scripts/search.py`，
  文档见本文件第 1–9 节。

## 1. 工作流 A 是什么

对科研材料目录完成 **批注提取 → 笔记结构化 → 知识关联 → 入库检索**
的确定性闭环。设计目标是解决阅读产物“抽得出来但用不了”的四类典型问题：

1. **锚点错位**：批注被挂到相邻段落或错误页 —— 每条高亮/划线的引文逐词恢复后在页文本中做
   exact / dehyphen / fuzzy 三级校验，逐条记录 `anchor_verified`。
2. **类型与颜色丢失**：高亮、下划线、页边批注、便签、手写墨迹分类型记录，颜色以 RGB/hex 保留。
3. **笔记退化为摘抄**：笔记带类别（问题/方法/结论/待验证疑点/可复用要点）与来源定位
   （文件+sha256+页+页内字符偏移+引文+bbox），可逐条回溯。
4. **版本串味**：重复下载（sha256 相同）与多版本（文件名规范化）自动分组，批注跨版本归并成簇。
5. **过程不可复现**：文件级 sha256、脚本自身 sha256、依赖版本、完整命令行、JSONL 日志全记录。

## 2. 安装与依赖

无需安装；脚本仅依赖 Python 3.8+ 标准库与工作流 B 的 poppler-utils。工作流 A 的 PDF
批注枚举按以下优先级自适应：

1. PyMuPDF（`fitz`）—— 若环境中已预装则直接使用（最佳路径，clip 恢复引文）。
2. pypdf —— 纯 Python；缺失时自动 `pip install pypdf` 重试（约数秒）。
3. poppler-only —— 仅有 pdftotext/pdftoppm 时降级：只做文本盘点，批注标注为“不可提取”并说明原因。

可选：tesseract（手写/扫描件 OCR）、Pillow（预览图）。缺失时相应产物降级但流程不中断。

## 3. 快速开始

```bash
# 主流程：盘点 -> 提取 -> 笔记 -> 关联 -> 索引 -> 证据
python3 scripts/pipeline.py --case /input/case.json --workdir /app/work --outdir /app/output

# 检索（单独使用亦可，指向任一由 pipeline 生成的 index.sqlite3）
python3 scripts/search.py --index /app/output/index.sqlite3 --q "machine learning potential" --explain
python3 scripts/search.py --index /app/output/index.sqlite3 --category doubt --since 2024-01-01 --format md
python3 scripts/search.py --index /app/output/index.sqlite3 --report /app/output/retrieval_report.md \
    --demo-queries "训练用了什么方法" "结论是否经过实验验证"
```

参数：`--materials DIR` 手工指定材料目录；`--engine auto|fitz|pypdf` 强制引擎；`--no-previews` 关闭预览渲染。

## 4. 输入目录约定

- 位置：优先 case.json 中的 `materials_dir`/`materials`/`data_dir`/`input_dir` 等键（含嵌套字符串）；
  缺省依次尝试 `/input/materials`、`/input/files`、`/input/data`、`/input`。
- `case.json` 与 `task.md` 不进入文件清单。
- 符号链接：记录目标与是否断链；内容读取走解析后的目标。

## 5. 输出产物（/app/output）

| 文件 | 内容 |
|-|-|
| `result.json` | 案例级结构化结果：case_id、status、summary、verification、output_contract 逐字段映射、sources、uncertainty、failures、changes、reproduction |
| `inventory.json` | 文件清单 + 元数据 + 重复组 + 版本组（含判定依据） |
| `notes.json` | 结构化笔记数组（schema 见 `../schemas/note.schema.json`） |
| `links.json` | 笔记关联 + 文件关联 + 归并簇 |
| `index.sqlite3` | 检索索引：files / annotations / notes_fts(FTS5) / links / file_links / events |
| `previews/` | 批注叠加预览 PNG（按类型着色），供人工核对锚点 |
| `run.log` | JSONL 操作与异常日志 |
| `verification_report.md` | 锚点校验率、失败与警告清单 |
| `digest.md` | 人类可读摘要 |
| `retrieval_report.md` | （可选）按问题生成的检索召回报告 |

## 6. 关键方法

- **引文恢复**：PyMuPDF 路径用 quad clip 精确取词；pypdf 路径用 poppler `-bbox` 词坐标 +
  凸四边形包含测试（QuadPoints 角点先按质心角排序，兼容 UL,UR,LL,LR 原始顺序）。
- **锚点校验**：页文本压缩空白后 exact 匹配 → 去断词匹配 → difflib 滑窗模糊匹配（≥0.85 判 fuzzy），
  记录页内字符偏移；失败逐条进日志，不静默通过。
- **笔记类别**：确定性关键词规则（中英双语）作用于批注文字优先、引文兜底。
- **关联**：实体抽取（DOI/arXiv/URL/引文/量/方法词表）+ 稀有度阈值 + 每节点度数上限，
  规则全文见 `../rules/link_rules.md`。
- **检索**：FTS5 + 中文二元组 LIKE 回退 + 结构化过滤（类别/标签/实体/来源/页/时间）。

## 7. 异常处理与自查

- 加密/损坏/权限不足/断链：逐一记录，流程继续，status 相应降为 partial。
- 无文本层扫描页：`no_text_layer` 标记 + OCR/几何锚点降置信度。
- 自查：核对 `verification.anchor_verification_rate`；抽查 preview PNG 框位；
  用 `result.json.reproduction.rerun` 逐字复跑对比。

## 8. 限制

不做文献检索、内容真伪判断与学术写作；版本判定不做全文相似度；OCR 质量受扫描分辨率限制；
CJK 检索为二元组级（词组级精度低于英文 FTS）。

## 9. 版本与维护

- v1.0.0（2026-11）。脚本 `PIPELINE_VERSION` 与 run.log 中的脚本 sha256 共同锁定可复现性。
- 改进方向：全文相似度版本聚类、更多学科方法词表、版式分栏感知排序、手写批注专用 OCR。
