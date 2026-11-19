# 输出布局与 result.json 字段说明

## /app/output 布局

```
/app/output/
├── result.json            # 结构化结果（契约字段 + case_id）
├── manifest.csv           # 每份输入文件一行：归属/动作/元数据/字段来源/标签
├── manifest.json          # 同上的 JSON 形式
├── README.md              # 人类可读摘要（目录树/统计/未解决项/冲突）
├── library/               # 整理后的资料库
│   └── <doc_type>/<year>/<规范文件名>
├── _archive/
│   ├── duplicates/        # 精确重复件（保留不删）
│   └── versions/<组键>/   # 被取代版本（预印本、旧版）
└── audit/
    ├── operations.jsonl   # 全量操作审计日志（追加式，跨多次运行）
    └── rollback.sh        # 一键回滚：bash audit/rollback.sh [OUTPUT_DIR]
```

## result.json 顶层字段

| 字段 | 含义 |
|-|-|
| case_id | 与 /input/case.json 一致（必填） |
| skill / skill_version | 本 Skill 标识 |
| status | success / partial / failed（有跳过、失败或冲突时必须为 partial） |
| generated_at | UTC 时间戳 |
| inputs | 输入目录、材料计数、清单指针 |
| outputs | library 根目录、manifest、审计日志、回滚脚本路径 |
| statistics | placed/archived/already_applied/conflict_renamed/skipped/failed 计数 |
| elements | **六类要素**：file_types、metadata_fields（覆盖率+缺失策略）、directory_levels、duplicates、version_relations、permissions_and_sources |
| sources | 字段证据位置说明（manifest 的 *_source 列、refs.json） |
| uncertainty | 不确定点清单（低置信提取、扫描件、加密件等） |
| change_log | 审计日志与回滚方式说明 |
| exceptions | skipped / failed / conflicts / issues 逐条记录 |
| output_contract | case.json output_contract 的**全部字段**：{delivered, note}；未交付为 null + 原因 |

## operations.jsonl 行格式

```json
{"ts":"...","src":"原相对路径","dst":"目标相对路径","action":"place|archive_duplicate|archive_version|skip","sha256":"...","status":"done|already_applied|conflict_renamed|skipped|failed","note":"依据"}
```

首行 `run_start`、末行 `run_end`（含统计）。日志追加式：重跑不丢历史，回滚脚本据此
覆盖历次运行落盘的全部文件。

## manifest.csv 关键列

`original_path, sha256, action, target_path, doc_type, tags, title, title_source,
first_author, year, venue, doi, arxiv, version_role, reason`

- `title_source` ∈ sidecar / pdf_text / pdf_text_heuristic / manual_verified / not_found
- `version_role` ∈ canonical / superseded / null
- `reason` 含规则编号（T0–T11），可追溯判定依据
