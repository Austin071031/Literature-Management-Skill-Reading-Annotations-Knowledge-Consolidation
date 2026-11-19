# 命名规则（自动命名模块）

## 1. 默认模板

`{first_author}-{year}-{title_slug}.<ext>`

示例：`Smith-2023-High-Throughput_Screening_of_Perovskite_Photocatalysts.pdf`

可用占位符：`{first_author}` `{year}` `{title_slug}` `{doi}` `{venue_slug}`。

## 2. 降级链（缺失字段逐段省略，绝不补造）

| 缺失 | 替换值 | 示例 |
|-|-|-|
| 作者 | `unknown_author` | `unknown_author-2021-Phase_Diagram.pdf` |
| 年份 | `ndate` | `Smith-ndate-Perovskite_Review.pdf` |
| 标题 | `untitled` | `Smith-2023-untitled.pdf` |
| 三者全缺 | 原文件名净化 + `__<sha256前8位>` | `scan_004__f884b278.pdf` |

全缺降级必然唯一且可追溯（sha 后缀），保证同批文件不互相覆盖。

## 3. plan_config.json 可覆盖字段

```json
{
  "library_root": "library",
  "directory_scheme": "by_type_year",   // by_type_year | by_type | flat
  "naming_template": "{first_author}-{year}-{title_slug}",
  "keep_archives": true,
  "max_title_slug": 60,
  "max_tags": 6
}
```

题面指定其他模板（如 `{doi}`、`{venue_slug}-{year}`）时，在 plan_config.json 中覆盖
`naming_template` 即可；占位符集合同上。

## 4. 字符与长度处理

- 非法文件名字符（`\/:*?"<>|` 与换行）→ 替换为空格后归一为下划线。
- Unicode NFKC 归一；保留中日韩文字（`\u4e00-\u9fff`）、字母、数字、`-_`。
- 连续下划线合并；超长标题截断到 `max_title_slug`（默认 60），并尽量截在词边界
  （最后一个 `_`，距边界 20 字符内才回退）。
- 扩展名沿用原扩展（小写）；无扩展名用 `.bin`。

## 5. 冲突处理（不覆盖原则）

- 计划内重名：首个文件保留原名，后续追加 `__<sha256前8位>`，再冲突追加 `_2/_3...`。
- 落盘时目标已存在：
  - 内容相同（sha256 一致）→ `already_applied`，幂等跳过；
  - 内容不同 → 追加 `__<sha256前8位>` 改唯一名，记入 `conflicts`，**不覆盖**。

## 6. 唯一性命名空间

重名判定按"目标目录 + 文件名"进行：canonical 版本（library/…）与被取代版本
（_archive/versions/…）允许同名，因为它们处于不同目录，同名反而便于识别版本关系。
