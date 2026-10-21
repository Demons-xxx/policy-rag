# 知识库样例与元数据说明

本目录给 PolicyRAG 的入库、检索、问答和验收提供一套**可直接使用的中文制度样例**。即使你第一次接触这个项目，按本文步骤也能新增一份制度并正确登记版本信息。

## 1. 目录里有什么

```
knowledge/
├── build_samples.py          # 重新生成下面 5 份样例的脚本（一般不用改）
├── README.md                 # 本文
└── samples/
    ├── XX大学实验室安全管理办法（2023年版）.pdf      # 旧版，已被替代
    ├── XX大学实验室安全管理办法（2025年修订）.docx   # 现行版，替代 2023 版
    ├── XX大学科研经费报销实施细则.docx               # 含报销标准表格
    ├── XX大学教职工请假与考勤管理规定.md             # 含审批层级表格
    ├── XX学院学生奖学金评定办法.docx                 # 含附件 1、附件 2 表格
    └── metadata.json                                # 5 份文档的登记信息
```

这 5 份文档覆盖了验收需要的场景：

| 文档 | 覆盖场景 |
| --- | --- |
| 实验室安全管理办法（2023 年版） | 旧版本、已被替代、PDF 原文预览、页内定位 |
| 实验室安全管理办法（2025 年修订） | 新版即时生效、新旧条款冲突（领用量、培训学时、检查频次、事故报告时限） |
| 科研经费报销实施细则 | 跨部门归口（教务处、科研院、财务处）、表格行入库、金额类问答 |
| 教职工请假与考勤管理规定 | Markdown 解析、审批层级表格、多轮追问 |
| 学生奖学金评定办法 | 附件表格、名额与金额、学院层面制度 |

## 2. 文件命名规范

统一使用：

```
《制度名称》（版本标识）.扩展名
```

实际文件名**不带书名号**，例如：

```
XX大学实验室安全管理办法（2025年修订）.docx
XX大学科研经费报销实施细则.docx
```

约定：

- 版本标识写“2023年版”“2025年修订版”等，放在中文括号里；没有版本可省略。
- 文件名里不要出现空格、斜杠 `/`、冒号 `:`、问号 `?` 等 Windows 非法字符。
- 一份制度一个文件；不要把多份制度拼进同一个文件。
- 支持格式：`.pdf`、`.docx`、`.md`、`.txt`。扫描版 PDF 无法提取文字，需要先做 OCR 或换成文字版。
- 文件是 UTF-8 编码；`md`/`txt` 用全角中文标点。
- 条款编号必须独立、完整：`第一章`、`第一节`、`第一条`、`（一）`、`附件1`。入库程序靠这些编号切分知识单元，编号缺失会导致整段归到上一章/条。
- 有表格的 DOCX 直接插入 Word 表格即可，入库程序会按“表格行”单独解析；不要用图片代替表格。

## 3. metadata.json 字段

`knowledge/samples/metadata.json` 是文档登记表。结构如下：

```json
{
  "schema": 1,
  "updated_at": "2025-09-01",
  "documents": [
    {
      "file": "XX大学科研经费报销实施细则.docx",
      "title": "XX大学科研经费报销实施细则",
      "doc_no": "校发〔2024〕38号",
      "version_label": "2024年版",
      "effective_from": "2024-05-01",
      "effective_to": null,
      "department": "教务处、科研院、财务处",
      "status": "active",
      "doc_id": "doc-09773b9ab828",
      "format": "docx",
      "pages": 1,
      "sha256": "09773b9ab828...",
      "supersedes": null
    }
  ]
}
```

各字段含义：

| 字段 | 谁填写 | 含义 |
| --- | --- | --- |
| `file` | 人工 | `samples/` 下的文件名，必须完全一致 |
| `title` | 人工 | 制度全称，会显示在引用和资料库中 |
| `doc_no` | 人工 | 文号，如 `校发〔2024〕38号`；没有就填 `null` |
| `version_label` | 人工 | 版本，如 `2024年版`、`2025年修订版` |
| `effective_from` | 人工 | 生效日期，格式必须是 `YYYY-MM-DD` |
| `effective_to` | 人工 | 废止日期，空值表示仍在施行；被替代时填旧版最后有效日（新版生效日前一天） |
| `department` | 人工 | 归口部门，多个部门用顿号分隔 |
| `status` | 人工 | `active` 现行 / `superseded` 已被替代 / `repealed` 已废止且无替代 |
| `supersedes` | 人工 | 本文件替代的旧文件 `doc_id`；没有就填 `null` |
| `doc_id` | 脚本 | `doc-` + 文件 sha256 前 12 位，由 `build_samples.py` 计算 |
| `format` | 脚本 | `pdf` / `docx` / `md` / `txt` |
| `pages` | 脚本 | PDF 实际页数；其他格式固定 1 |
| `sha256` | 脚本 | 文件内容哈希，用于去重和稳定 `doc_id` |

> 注意：`build_samples.py` 会按内置的 5 份文档**重写** `metadata.json`。如果你手工新增了别的文档，不要直接重跑它；请把新文档的登记项手工写进 `metadata.json`，或把内容补进脚本后再生成。

## 4. 如何新增一份制度（零经验版）

以新增《XX大学大型仪器设备开放共享管理办法》为例：

1. **准备文件**
   - Word 另存为 `.docx`，或导出为文字版 `.pdf`，或直接写 `.md`。
   - 打开文件通读一遍：确认每章、每条都有编号，表格是真正的表格而不是图片。

2. **命名并放入目录**
   - 文件名：`XX大学大型仪器设备开放共享管理办法.docx`
   - 放到 `knowledge/samples/`。

3. **计算哈希和 doc_id**
   在项目根目录运行：

   ```powershell
   .venv\Scripts\python.exe -c "import hashlib,pathlib; p=pathlib.Path(r'knowledge/samples/XX大学大型仪器设备开放共享管理办法.docx'); print('doc-'+hashlib.sha256(p.read_bytes()).hexdigest()[:12])"
   ```

   输出形如 `doc-1a2b3c4d5e6f`，记下来。

4. **在 metadata.json 的 `documents` 数组里加一项**

   ```json
   {
     "file": "XX大学大型仪器设备开放共享管理办法.docx",
     "title": "XX大学大型仪器设备开放共享管理办法",
     "doc_no": "校发〔2026〕3号",
     "version_label": "2026年版",
     "effective_from": "2026-03-01",
     "effective_to": null,
     "department": "实验室与设备管理处",
     "status": "active",
     "doc_id": "doc-1a2b3c4d5e6f",
     "format": "docx",
     "pages": 1,
     "sha256": "把完整 sha256 填在这里",
     "supersedes": null
   }
   ```

   保存前检查 JSON：逗号、引号、日期格式都不能错。

5. **上传入库**
   - 打开 Web 界面 → 资料库 → 上传资料，选择文件并填写标题、文号、版本、生效日期、归口部门；
   - 或用接口（`metadata` 是 JSON 字符串）：

     ```bash
     curl -X POST http://127.0.0.1:7880/api/ingest \
       -F "files=@knowledge/samples/XX大学大型仪器设备开放共享管理办法.docx" \
       -F 'metadata={"title":"XX大学大型仪器设备开放共享管理办法","doc_no":"校发〔2026〕3号","version_label":"2026年版","effective_from":"2026-03-01","department":"实验室与设备管理处","status":"active"}'
     ```

6. **发布并验证**
   - 入库任务完成后调用 `POST /api/publish`（或在界面触发发布）。
   - 打开 `GET /api/documents`，确认标题、文号、状态、`has_pdf` 正确。
   - 问一个只有这份制度才有的问题，回答应带 `[证据n]`，点引用能看到原文。

## 5. 如何标注“废止”和“被替代”

### 5.1 新版替代旧版（最常见）

以本目录的实验室安全管理办法为例，操作顺序是**先旧后新**：

1. 先入库 2023 年版，拿到它的 `doc_id`（本例 `doc-4922e477e522`）。
2. 把 2023 年版登记改为：
   - `status`: `superseded`
   - `effective_to`: `2025-08-31`（新版生效日 `2025-09-01` 的前一天）
3. 新增 2025 年修订版登记：
   - `status`: `active`
   - `effective_from`: `2025-09-01`
   - `supersedes`: `doc-4922e477e522`（指向上面的旧 `doc_id`）
4. 两份都重新入库并发布。
5. 提问旧版才有的数值（如“单次领用不超过几天”），回答应以新版为准，并在检索备注里提示旧版已被替代。

### 5.2 制度直接废止且没有新版

- 把 `status` 改为 `repealed`；
- `effective_to` 填废止日期；
- `supersedes` 保持 `null`。

检索层会排除 `repealed` 文档；`superseded` 文档仍可能被检索到，但必须提示“旧版、仅供参考”。

### 5.3 只改一两个条款的修订

仍按“新版替代旧版”处理：新文件单独存一份、单独登记，不要直接覆盖旧文件。覆盖会改变 `sha256` 和 `doc_id`，旧引用会失效。

## 6. 常见问题

**Q：PDF 能选中文字，但入库后是空的？**
A：多为扫描件。用 Word 重新导出文字版 PDF，或先 OCR。可用下面命令检查：

```powershell
.venv\Scripts\python.exe -c "import fitz; d=fitz.open(r'knowledge/samples/XX.pdf'); print(len(d), sum(len(p.get_text().strip()) for p in d))"
```

页数大于 0 且提取字符数明显大于 0 才算正常。

**Q：同一份文件改了一个字，需要重新登记吗？**
A：需要。内容变了 `sha256` 就变，`doc_id` 也会变；请按新版/修订处理，不要复用旧登记。

**Q：能直接改 samples 里的文件吗？**
A：可以用于本地试验，但改完要同步更新 `metadata.json` 的 `sha256`、`doc_id`、`pages`；否则登记信息和实际文件对不上。

**Q：为什么 PDF 里的页码从 1 开始？**
A：样例 PDF 由 `knowledge/build_samples.py` 用 PyMuPDF + 微软雅黑生成，页脚为“第 X 页 / 共 N 页”，正文可被 `fitz` 正常提取，便于验证引用页码。

**Q：想重建这 5 份样例？**
A：

```powershell
.venv\Scripts\python.exe knowledge\build_samples.py
```

脚本会重新生成 5 份文件（内容固定）、重算哈希并重写 `metadata.json`。重建后请重新入库/发布。
