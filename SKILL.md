---
name: annotation-knowledge-sink
description: 科研资料整理与阅读批注知识沉淀双工作流 Skill。工作流 B（文档库整理）：盘点材料目录、提取文献元数据（标题/作者/年份/DOI/arXiv/关键词，逐字段带证据与置信度）、类型分类、版本分组与重复归档、受控命名、审计化入库（复制非剪切、幂等、可回滚）。工作流 A（批注与知识沉淀）：提取 PDF 批注（高亮/划线/页边批注/便签/手写标记，含颜色、作者、时间），逐词恢复引文并在页文本中做锚点校验，结构化为带来源定位的笔记（问题/方法/结论/待验证疑点/可复用要点），建立带依据的可解释关联与跨版本批注归并，生成 SQLite 可检索索引与召回报告。当任务涉及整理科研文件、维护元数据/版本关系、提取批注、结构化笔记或检索知识沉淀时使用。触发示例：给定材料目录要求“盘点并整理文献库”“提取这些 PDF 的批注并做成可检索笔记”“处理重复下载与多版本文献”。
---

# annotation-knowledge-sink：科研资料整理与阅读批注沉淀

本 Skill 提供两条确定性流水线，覆盖题目的完整闭环。重型工作全部由自带脚本完成；
Agent 的职责是**读取输入约定、按需运行流水线、审阅产出并诚实标注**，不是手写答案。

- **工作流 B：文档库整理**（inventory → extract_refs → plan → apply → report → validate）
  回答：目录里有什么、每篇文献的元数据是什么、哪些是重复/多版本、按什么规则命名归档、
  操作证据与回滚在哪里。
- **工作流 A：批注与知识沉淀**（pipeline.py → search.py）
  回答：批注能否逐条回到原文的具体页与位置、笔记论断是否有原文支撑、条目能否被真实问题
  检索到并可复跑核对。

两条工作流独立可用、可先后运行；后运行者自动合并进 `/app/output/result.json`（A 挂到
`annotations_workflow` 键下），最终保留 case_id 与 output_contract 全字段。

## 适用条件

输入为材料目录（PDF 为主，可含 docx/html/txt/md 剪藏、BibTeX/RIS 侧车、散落笔记），
位置由 `/input/case.json` 指定或可自动发现。任务属于资料组织、元数据/版本维护、批注提取、
笔记结构化、知识关联或检索之一。与上述无关时不要调用。

## 依赖与环境自适应

| 依赖 | 用途 | 缺失时 |
|-|-|-|
| Python 3.8+ 标准库 | 全部脚本 | 必需 |
| poppler-utils（pdftotext/pdftoppm/pdfinfo） | 页文本/词坐标/渲染 | 相关步骤记失败并说明 |
| PyMuPDF 或 pypdf | 工作流 A 批注枚举 | 自动尝试 `pip install pypdf`；再失败降级为文本盘点并如实标注“批注不可提取” |
| tesseract（可选） | 手写/扫描件 OCR | 缺失时保留区域截图 + 低置信度标注 |
| Pillow（可选） | 批注叠加预览图 | 缺失时跳过预览并在日志注明 |

脚本自动探测并记录实际引擎与库版本；同一输入在同一引擎下输出仅时间戳不同。

## 执行总流程（Agent 按此操作）

`<SKILL>` 为 Skill 安装目录（评测时 `/skills/weekly-skill`）。预算提示：两条流水线合计通常
< 2 分钟；900 秒时限内应尽早保存结果、增量改进。

### 步骤 1：读取输入约定（约 1 次模型调用）

1. 读 `/input/case.json`（取 `case_id`、`output_contract`、材料目录线索）与 `/input/task.md`。
2. 浏览材料目录：确认文件类型、重复/多版本/符号链接别名、加密或损坏迹象、是否有 BibTeX/RIS
   侧车。**一切以实际文件为准，不凭标签假设内容。**
3. 判断任务侧重，决定运行 B、A 或两者（多数案例题两者都跑；时间不足优先跑 B 保住整理闭环，
   再跑 A 增补批注沉淀）。

### 步骤 2：运行工作流 B（文档库整理，0 次模型调用）

```bash
bash <SKILL>/scripts/run_pipeline.sh /input /app/work /app/output
# 内部依次: inspect_case -> inventory -> extract_refs -> plan -> apply -> report -> validate_output
# 若 task.md 对命名/目录方案有明确要求，可先写 /app/work/plan_config.json 再:
bash <SKILL>/scripts/run_pipeline.sh /input /app/work /app/output /app/work/plan_config.json
```

设计要点（详见 `docs/USAGE.md` 与 `references/output_schema.md`）：
- 只复制不剪切：`/input` 源文件绝不修改；输出写入 `/app/output/<library_root>`。
- 元数据铁律：只提取原文或侧车中真实存在的字段，提取不到为 null 并注明原因，绝不补造；
  每个字段带 source 与 confidence。
- 版本：DOI 与（规范化标题+第一作者）并查集分组，退化用规范化标题；canonical 打分保留，
  其余归入 `_archive/versions/<组>`；sha256 精确重复归入 `_archive/duplicates`。
- 命名：`{first_author}-{year}-{title_slug}` 模板降级链（缺作者/年份/标题逐段省略，
  全缺回退净化原名+短哈希）；命名空间按目标目录隔离，目标冲突绝不覆盖（追加短哈希）。
- 审计：`audit/operations.jsonl` 逐操作记录（含 sha256、动作、依据 rule_id），
  `audit/rollback.sh` 一键回滚；幂等重跑已应用操作记 `already_applied`。
- 异常：损坏/加密/无文本层扫描件/权限不足逐类处置并如实记录，单文件失败不中断。
- 机读自查：`validate_output.py` 校验契约字段、哈希一致、审计完备，结果写
  `/app/work/selfcheck.json`（0 通过 / 1 发现问题）。

### 步骤 3：运行工作流 A（批注与知识沉淀，0 次模型调用）

```bash
python3 <SKILL>/scripts/pipeline.py --case /input/case.json --workdir /app/work --outdir /app/output
```

产出（与 B 的产物共存于 /app/output）：`inventory.json`（含批注级清单）、`notes.json`、
`links.json`、`index.sqlite3`、`previews/`（批注叠加预览 PNG）、`run.log`、
`verification_report.md`、`digest.md`、`result_annotations.json`；
若 B 已生成 result.json，自动合并（本工作流整体挂入 `annotations_workflow` 键）。

- 批注类型区分（不混为一类）：highlight / underline / squiggly / strikeout / margin_note
  （FreeText）/ sticky_note / handwritten_mark（Ink，区域渲染 + 可选 OCR）/ form_field /
  shape / line_mark / stamp / attachment / media；颜色以 `color_hex` 忠实记录。
- 锚点校验：每条高亮/划线的引文逐词恢复（PyMuPDF clip 或 poppler 词坐标 + 凸四边形包含），
  在页文本中 exact / dehyphen / fuzzy 三级校验，逐条记录 `anchor_verified` 与页内字符偏移；
  失败不静默通过，进日志。
- 笔记 schema 见 `schemas/note.schema.json`；类别（question/method/conclusion/doubt/
  reusable/keypoint）由确定性规则判定；每条笔记可经 `annotation_id` 或 文件+页+字符偏移回溯。
- 关联规则见 `rules/link_rules.md`：共享 DOI/arXiv → same_reference；稀有实体（df≤3）或 ≥2
  实体 → related；同文件共现 → co_mention；每节点保留最强 12 条防过度连接；版本组内相同
  引文的批注聚成 `cluster_id` 归并簇。

### 步骤 4：检索留证（0 次模型调用）

把 task.md 中的检索类问题改写成关键词，运行并生成召回报告：

```bash
python3 <SKILL>/scripts/search.py --index /app/output/index.sqlite3 \
  --report /app/output/retrieval_report.md --demo-queries "<问题1关键词>" "<问题2关键词>"
```

支持 `--q`（FTS5，中文自动二元组回退）、`--category/--tag/--entity/--source/--page/--since/--until`、
`--explain`。召回不足与低置信度命中在报告中自动标注；Agent 复核后可补人工判断。

### 步骤 5：审阅并定稿 result.json（1–2 次模型调用）

1. 核对 result.json 的 `case_id` 与 `output_contract`：**逐字段**将 `pending_agent_mapping`
   占位（B/A 两条流水线都会自动映射一部分键名，如 inventory/annotations/notes/index/status）
   替换为真实结果引用；契约中没有对应产物的字段如实标 `"not_applicable"` 及原因；不得删字段、
   不得编造值。
2. `status` 诚实取值：success / partial / failed；`exceptions`（或 `failures`）、
   `uncertainty`、`change_log` 必须保留。
3. 抽查 2–3 条笔记（用 char_start/char_end 或引文回原文页核对）与 1 张 preview PNG（框位），
   抽查 1–2 个 library 目标文件 sha256 与源一致；结论写入 `agent_review` 字段：
   `{"spot_check": "pass/fail 细节", "reviewed_at": "ISO时间"}`。
4. 复查 `selfcheck.json` 与 `verification_report.md`，确认没有把未完成写成完成。

### 步骤 6：收尾

确认 `/app/output` 产物齐备、result.json 合法 JSON 且含 case_id 与全部契约字段。
时间不足时保留已写出产物并确保 status/limits 如实——两条流水线任何阶段失败都不会留下
无声的中间状态。

## 异常处置总表

| 情况 | B 工作流 | A 工作流 |
|-|-|-|
| 加密 PDF | 记 unreadable/encrypted，跳过元数据提取 | 尝试空密码；失败记 failure，仍入库存 |
| 损坏文件 | 记 corrupted，跳过 | 捕获异常记 failure + traceback 摘要，不影响其他文件 |
| 无文本层扫描件 | 类型判 scanned_document | 标记 no_text_layer，锚点走 OCR/几何并降置信度，写入 uncertainty |
| OCR 置信度低/无 tesseract | — | 手写批注保留截图与 bbox，置信度 0.2–0.4，明确标注 |
| 重复文件 | sha256 分组，归档 duplicates | 同左 + 批注跨文件归并簇 |
| 多版本 | 并查集分组，归档 versions | 同左 + 相同引文批注 cluster 归并 |
| 权限不足/断链别名 | 记录权限与链接目标，跳过 | 同左 |
| 命名冲突 | 追加短哈希，绝不覆盖 | — |
| 材料目录缺失 | 预检报 issues，result 标 failed | 退出码 2，result 标 failed 与已检查位置 |

## 输出 schema 摘要

`/app/output/result.json`：B 产出顶层含 `case_id / skill / status / inputs / outputs /
statistics / elements / sources / uncertainty / change_log / exceptions / output_contract`；
运行 A 后追加 `annotations_workflow`（其内部含 summary、verification、deliverables、
failures、reproduction 等）。细节见 `references/output_schema.md` 与
`schemas/note.schema.json`。

## 最小调用示例（示意输出，非正式案例结果）

输入 `/input/case.json`：`{"case_id": "demo-001", "output_contract": {"inventory": "...",
"annotations": "...", "notes": "...", "index": "...", "status": "..."}}`，材料目录含
`paper_v1.pdf`（带 1 条高亮）与 `paper_v2.pdf`（同名新版本）。

```bash
bash /skills/weekly-skill/scripts/run_pipeline.sh /input /app/work /app/output
# -> /app/output/library/... 归档结构 + manifest.csv + result.json（status 示例值: partial）
python3 /skills/weekly-skill/scripts/pipeline.py --case /input/case.json --outdir /app/output
# -> 合并进 result.json 的 annotations_workflow；verification.anchor_verification_rate 示例值: 1.0
python3 /skills/weekly-skill/scripts/search.py --index /app/output/index.sqlite3 \
  --q "active learning" --format md
# -> 命中笔记列表，每条含 来源文件+页码+anchor_verified（示例输出，实际值以真实运行为准）
```

## 维护信息

- 版本 1.0.0（2026-11）。变更见 `CHANGELOG.md`；各脚本文件头注明用途与用法。
- 复现：所有操作带 sha256 与时间戳；`audit/rollback.sh` 回滚 B 的全部复制；
  `result.json`（B）与 `annotations_workflow.reproduction`（A）给出逐字复跑命令。
- 限制：不做文献检索、内容真伪判断与学术写作；版本判定不做全文相似度；OCR 质量受扫描
  分辨率限制；CJK 检索为二元组级召回。
