# 工具函数参考文档

> 本文件描述了 LLM 编排流程中可调用的所有工具函数。
> 工具通过 bash 调用 Python 脚本，LLM 负责传参和处理返回值。

---

## 调用方式

所有工具统一通过 bash 命令调用：

```bash
cd <skill_dir> && python -m scripts.tools <tool_name> '<args_json>'
```

返回值为 JSON 字符串（stdout），直接解析即可。

**错误处理**：失败时返回 `{"success": false, "error": "错误描述"}` 或 `{"error": "错误描述"}`。
非零退出码也表示失败。

---

## 概览

| 工具名 | 用途 | 何时调用 |
|--------|------|----------|
| `get_config` | 获取系统配置 | 步骤 0 |
| `get_ocr_names` | 获取 OCR 识别名单 | 步骤 1.2 |
| `collect_files` | 收集类别目录文件 | 步骤 1.1 |
| `collect_source_candidates` | 收集来源目录候选附件 | 步骤 1.3 / 1.5.1 |
| `lookup_invoice_details` | 查询发票 OCR 详情 | 步骤 1.4 |
| `extract_attachment_text` | OCR 提取附件文字 | 步骤 1.4 |
| `lookup_accommodation_standard` | 查询住宿费标准 | 住宿类检查 |
| `check_seat_class` | 检查座位/舱位标准 | 出差交通类检查 |
| `copy_file` | 复制文件到类别目录 | 步骤 1.5.1 |
| `generate_meal_doc` | 生成加班餐情况说明 | 步骤 1.5.2 |
| `fix_meal_doc` | 修复加班餐情况说明 | 步骤 1.5.3 |
| `merge_meal_docs` | 合并加班餐说明文件（含已有附件） | 步骤 2 |
| `save_attachment_report` | 写入数据库 | 步骤 3 |
| `update_invoice_category` | 更新发票分类类别 | 步骤 1.5（类别纠正时） |
| `backup_file` | 备份原始文件 | 需要时 |

---

## 详细说明

### get_config

获取系统配置信息。

```bash
python -m scripts.tools get_config '{}'
```

```
返回: {
  "name_list": ["张三", "李四", ...],
  "source_root": "/path/to/课题组成员文件",
  "invoice_root": "/path/to/发票分类",
  "categories": ["打车", "出差", "加班餐", "打印", "快递", "材料"],
  "overtime_meal_output_dir": "/path/to/加班餐输出",
  "cache_dir": "/path/to/cache"
}
```

---

### get_ocr_names

获取已被百度 OCR 成功识别为发票的文件名集合。

```bash
python -m scripts.tools get_ocr_names '{}'
```

```
返回: {
  "ocr_names": ["张三+50+滴滴发票.pdf", "李四+80+出租车.pdf", ...]
}
```

**用途**：区分发票与附件。文件名在此集合中 = 发票，不在 = 附件。

---

### collect_files

收集指定类别目录下的所有文件。

```bash
python -m scripts.tools collect_files '{"category":"打车"}'
```

```
返回: [
  {
    "name": "张三+50+滴滴发票.pdf",
    "full_path": "/absolute/path/to/file",
    "parent": "张三",
    "person": "张三"
  },
  ...
]
```

**注意**：返回的是该类别目录下的所有文件（包括发票和附件），由 LLM 根据
OCR 名单进一步区分。

---

### collect_source_candidates

从来源目录（`source_root/<人名>/`）收集候选附件文件。

```bash
python -m scripts.tools collect_source_candidates '{"person":"张三"}'
```

```
返回: [
  {
    "name": "张三行程单.jpg",
    "full_path": "/path/to/课题组成员文件/张三/张三行程单.jpg",
    "parent": "张三",
    "person": "张三"
  },
  ...
]
```

**过滤条件**（工具内部处理）：
- 排除已在 OCR 识别名单中的文件（即排除发票）
- 排除本次运行中已被匹配使用过的文件

---

### lookup_invoice_details

从发票数据库中查询发票的 OCR 识别详情。

```bash
python -m scripts.tools lookup_invoice_details '{"filename":"张三+50+滴滴发票.pdf"}'
```

```
返回: {
  "价税合计": "50.00",
  "销售方名称": "滴滴出行科技有限公司",
  "商品名称": "客运服务费",
  "商品单价": ["50.00"],
  "购方名称": "山东大学",
  "发票类型": "增值税电子普通发票",
  "开票日期": "2024年03月12日",
  "匹配简介": "张三+50+打车",
  "姓名/公司": "张三"
}
```

**缺失字段**：未识别到的字段值为空字符串。

---

### extract_attachment_text

对任意类型的附件文件进行截图+OCR文字提取。

```bash
python -m scripts.tools extract_attachment_text '{"filepath":"/path/to/行程单-张三.pdf"}'
```

```
返回: {
  "text": "滴滴出行 行程明细\n乘车人：张三\n起点：济南西站\n终点：山东大学\n...",
  "truncated": false,
  "method": "pdf_ocr"
}
```

**支持的文件类型**：
- 图片 (.jpg/.png/.bmp/…) → 直接 OCR
- PDF → PyMuPDF 渲染为图片后 OCR（最多 3 页）
- docx → python-docx 直接读文本
- doc → 自动转换为 .docx 后读文本，失败则转 PDF 再 OCR

**返回 null**：文件不存在、格式不支持、或 OCR 失败时返回 null。

---

### lookup_accommodation_standard

查询指定省份/城市的住宿费标准限额。

```bash
python -m scripts.tools lookup_accommodation_standard '{"province":"山东省","person":"张三","month":8}'
```

```
返回: {
  "province": "山东省",
  "category_level": 3,
  "category_label": "三类",
  "is_peak": true,
  "peak_period": "7-9月",
  "limit": 480,
  "base_limit": 400,
  "special_person": null
}
```

**参数说明**：
- `province`（必填）：省份或城市名称，支持模糊匹配（如"山东"匹配"山东省"）
- `person`（可选）：报销人姓名，用于查询是否为特例人员（享受更高类别标准）
- `month`（可选）：入住月份（1-12），用于判断是否旺季。不传则不判断旺季。

**特例人员**：若 `person` 匹配特例人员表，返回其对应的住宿类别标准（如陈阿莲 → 一类）。

---

### check_seat_class

检查出差交通发票的座位/舱位是否符合标准。

```bash
python -m scripts.tools check_seat_class '{"person":"张三","seat_info":"二等座","transport_type":"高铁"}'
```

```
返回: {
  "person": "张三",
  "seat_info": "二等座",
  "standard": "二等座",
  "pass": true,
  "special_person": null,
  "message": "符合座位标准"
}
```

**参数说明**：
- `person`（必填）：报销人姓名
- `seat_info`（必填）：从发票 OCR 提取的座位信息（如"二等座"、"一等座"、"商务座"、"经济舱"等）
- `transport_type`（必填）：交通类型，可选值："高铁"、"动车"、"火车"、"飞机"

**判定逻辑**：
- 高铁/动车默认标准：二等座。一等座、商务座均超标。
- 飞机默认标准：经济舱。商务舱、头等舱均超标。
- 特例人员（如陈阿莲：二级教授）→ 高铁可报一等座。
- 超标时 `pass` 为 false，`message` 说明超标详情。

---

### copy_file

将来源目录中的附件复制到类别目录。对 `.doc` 格式文件自动转换为 `.docx` 后再复制。

```bash
python -m scripts.tools copy_file '{"src":"/path/to/源文件","dst_dir":"/path/to/类别目录","mark_used":true}'
```

```
返回: {
  "success": true,
  "dst_path": "/path/to/类别目录/文件名.docx",
  "dst_name": "文件名.docx",
  "converted_from_doc": true
}
```

**`.doc` 自动转换**：当源文件为 `.doc` 格式时，工具会自动通过 LibreOffice 将其
转换为 `.docx` 格式，并将转换后的 `.docx` 文件复制到目标目录。返回的 `dst_path`
指向转换后的 `.docx` 文件。`converted_from_doc` 字段标识是否发生了格式转换。

---

### generate_meal_doc

自动生成加班餐情况说明（山东大学科研业务专项经费使用说明表）。

```bash
python -m scripts.tools generate_meal_doc '{"person":"张三","amount":29.5,"seller":"美团外卖","commodity":"餐饮","invoice_filename":"张三+29.5+美团.pdf"}'
```

```
返回: {
  "generated_path": "/path/to/张三+29.5+加班餐.docx",
  "persons_text": "1人，张三",
  "success": true
}
```

**可选参数**：
- `name_list`：课题组名单，不传则从配置中获取
- `reason`：加班事由，不传则自动生成默认文本

**内部逻辑**：
- 人数 = ceil(amount / 30)
- 报销人排首位
- 从 name_list 随机补齐人数

---

### fix_meal_doc

修复已存在但校验不通过的加班餐情况说明。

```bash
python -m scripts.tools fix_meal_doc '{"original_path":"/path/to/原始说明.docx","invoice_filename":"张三+29.5+美团.pdf","person":"张三","amount":29.5,"target_persons":["张三","李四"],"required_count":1}'
```

```
返回: {
  "fixed_path": "/path/to/修复后.docx",
  "fix_msg": "补充人数1→2，修正金额",
  "success": true
}
```

**可选参数**：`reason_text`（事由文本）

**注意**：修复前自动备份原文件到 `cache/attachment_backup/`。

---

### merge_meal_docs

将多个加班餐情况说明合并为一个文件（最多 6 个一组）。
自动将 `.doc` 格式文件转换为 `.docx` 后再合并。
支持同时传入生成的文件和已有的附件文件，统一格式后一并合并。

```bash
python -m scripts.tools merge_meal_docs '{"generated_files":["/path/to/file1.docx","/path/to/file2.doc"], "existing_meal_files":["/path/to/existing.doc"]}'
```

```
返回: {
  "merged_paths": ["/path/to/合并后.docx"],
  "merge_map": {
    "/path/to/file1.docx": "/path/to/合并后.docx",
    "/path/to/file2.docx": "/path/to/合并后.docx",
    "/path/to/existing.doc": "/path/to/合并后.docx"
  },
  "success": true
}
```

**参数说明**：
- `generated_files`（必填）：本次 generate_meal_doc / fix_meal_doc 生成的文件路径列表
- `existing_meal_files`（可选）：已有的加班餐说明附件路径列表（检查通过的 .doc/.docx 文件）。
  这些文件会被复制到输出目录后参与合并，源目录中的原始文件不会被修改或删除。
  通过合并统一格式，解决 .doc→.docx 转换后的字体问题。

---

### save_attachment_report

将附件检查结果写入记录数据库。

```bash
python -m scripts.tools save_attachment_report '{"results":[{"旧文件名":"张三+50+滴滴发票.pdf","附件状态":"附件齐全","缺少类型":"","匹配附件":"张三行程单.jpg","附件路径":"/path/to/...","生成文件":"","校验详情":"已匹配行程单","附件类别":"打车"}]}'
```

```
返回: {
  "success": true,
  "records_written": 15,
  "anomalies_written": 3
}
```

**results 数组每项的字段**：

| 字段 | 说明 | 示例 |
|------|------|------|
| `旧文件名` | 发票文件名 | `"张三+50+滴滴发票.pdf"` |
| `附件状态` | 判定结果 | `"附件齐全"` / `"缺少附件"` / `"已自动生成"` / `"附件已修复"` / `"附件校验不通过"` |
| `缺少类型` | 缺少的附件类型 | `"行程单"` / `"转账截图"` / `""` |
| `匹配附件` | 匹配到的附件文件名 | `"张三行程单.jpg"` |
| `附件路径` | 附件完整路径 | `"/path/to/..."` |
| `生成文件` | 自动生成的文件路径 | `"/path/to/..."` 或 `""` |
| `校验详情` | 人可读的校验信息 | `"已匹配行程单"` |
| `附件类别` | 所属检查类别 | `"打车"` |

**内部处理**：
- 通过 `records.匹配发票` 反查对应报销记录
- 多张发票对应同一记录时自动聚合（取最严重状态）
- 异常项自动追加到 `records.校验详情`
- 非 PDF/图片格式的附件自动转为 PDF

---

### update_invoice_category

更新发票在数据库中的分类类别。用于附件检查阶段发现分类错误时纠正
（如出差目录下的济南本地打车发票应归为打车）。

```bash
python -m scripts.tools update_invoice_category '{"filename":"张三+50+滴滴发票.pdf","new_category":"打车","reason":"行程单显示起止点均在济南市内，属于本地打车"}'
```

```
返回: {
  "success": true,
  "old_category": "出差",
  "new_category": "打车",
  "reason": "行程单显示起止点均在济南市内，属于本地打车",
  "file_moved": true
}
```

**参数说明**：
- `filename`（必填）：发票文件名（旧文件名）
- `new_category`（必填）：新类别名称（必须是有效的数据库类别，如"打车"、"出差"等）
- `reason`（可选）：纠正原因，会记入校验详情

**内部处理**：
- 同时更新 `invoices.db` 和 `records.db` 中的 category 字段
- 自动将文件从旧类别目录移动到新类别目录
- 更新数据库中的文件路径

**典型使用场景**：
- 出差目录下发现打车发票，行程单显示济南本地行程 → 纠正为「打车」
- 打车目录下发现出差打车发票，行程单显示异地行程 → 纠正为「出差」

---

### backup_file

将文件备份到缓存目录。

```bash
python -m scripts.tools backup_file '{"filepath":"/path/to/要备份的文件","delete_original":true}'
```

```
返回: {
  "backup_path": "/path/to/cache/attachment_backup/文件名.docx",
  "success": true
}
```

---

### update_invoice_category

修改发票在数据库中的分类类别。当附件检查过程中发现发票被错误分类时
（如打车实际为出差打车，或出差中的打车实际为本地打车），调用此工具修正。

```bash
python -m scripts.tools update_invoice_category '{"filename":"张三+50+滴滴发票.pdf","new_category":"出差","reason":"行程单显示目的地为北京"}'
```

```
返回: {
  "success": true,
  "old_category": "打车",
  "new_category": "出差",
  "moved": true,
  "new_path": "/path/to/出差/张三/张三+50+滴滴发票.pdf",
  "reason": "行程单显示目的地为北京"
}
```

**参数说明**：
- `filename`（必填）：发票文件名（旧文件名）
- `new_category`（必填）：新的数据库类别，仅限 打车/出差/加班餐/打印/快递/材料
- `reason`（可选）：修正原因说明

**内部处理**：
- 更新 invoices.db 和 records.db 中的 category 字段
- 将文件从旧类别目录移动到新类别目录（保持人名子目录结构）
- 更新数据库中的 full_path

**使用场景**：
- 「打车」类别中的发票，行程单 OCR 显示目的地在外地 → 改为「出差」
- 「出差」类别中的网约车发票，行程单 OCR 显示起止点均在济南 → 改为「打车」

---

## 错误处理

所有工具在执行失败时会返回：
```json
{
  "success": false,
  "error": "错误描述信息"
}
```

**LLM 的错误处理策略**：
1. 工具返回失败时，记录错误并继续处理下一项（不要整体中断）
2. OCR 提取失败时，退化为仅根据文件名判断
3. 生成/修复 docx 失败时，将状态标记为"缺少附件"
4. 数据库写入失败时，向用户报告并建议重试
