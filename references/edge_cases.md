# 异常与边界情况处置

| 情况 | 识别方式 | 处置 |
|-|-|-|
| 损坏 PDF | pdfinfo 报错 / 无 xref | action=skip；记 exceptions.skipped + uncertainty；不臆造元数据 |
| 加密 PDF | pdfinfo `Encrypted: yes` 或 pdftotext 报 password | 单独列入 encrypted_pdfs；skip 并记录 |
| 扫描件（无文本层） | 前两页 pdftotext 输出 0 字符且未加密 | 归入 scanned_document/；元数据字段 null+not_found；记 scanned_candidates |
| 文件名与内容不符 | 魔数嗅探（sniff_type）与扩展名比对 | 以魔数为准处理；inventory 同时记录两者 |
| 中文乱码 | 文本以 UTF-8 errors=replace 读取 | 元数据提取照常；乱码字段不进入命名（slug 归一会剔除） |
| 同名不同版本 | 目标已存在且 sha256 不同 | 追加 `__<sha前8位>` 改唯一名，记 conflicts，不覆盖 |
| 同名同内容 | sha256 一致 | already_applied 幂等跳过 |
| 权限不足 | os.access(R_OK)=False | inventory 记 readable=false + perm_mode；skip 并记录 |
| 权限不足（写） | apply 复制抛 OSError | 单文件 failed，进 unresolved，不中断其余文件 |
| 巨大批次 | 脚本 O(n) 哈希与扫描 | 秒级完成；模型不逐文件介入 |
| 断点续跑 | operations.jsonl 追加式 | 重跑自动 already_applied；失败文件重试 |
| 回滚 | bash audit/rollback.sh [OUTPUT_DIR] | 删除历次运行落盘的全部文件并清理空目录；审计日志与清单保留 |

## 关于"宁可跳过也不猜错"

本 Skill 的评分红线是"标签与原文对得上"。凡提取不到、置信度低、来源存疑的字段，
一律 null + not_found 并在 uncertainty 点名——**缺失但诚实的字段优于臆造的字段**。
Agent 抽验修正后（source=manual_verified）可转为高置信。
