# PolicyRAG 政策制度智能问答

> 上传制度文件就能问：条款原文、办事流程、适用范围，回答附带原文出处，点一下就能看到 PDF 原页。
> 本 README 面向**零经验**的协作者，照着从上往下做即可；每一步都写了「怎么验证成功」。

---

## 目录

1. [这个项目能做什么](#1-这个项目能做什么)
2. [它是怎么工作的](#2-它是怎么工作的)
3. [目录结构](#3-目录结构)
4. [快速开始（15 分钟）](#4-快速开始15-分钟)
5. [日常使用](#5-日常使用)
6. [常见问题](#6-常见问题)
7. [开发指南](#7-开发指南)
8. [Git 协作](#8-git-协作)
9. [已知限制与后续计划](#9-已知限制与后续计划)

---

## 1. 这个项目能做什么

PolicyRAG 是一个「政策 / 制度 / 办事流程」知识库问答系统。你把学校的制度文件（PDF、Word、Markdown、TXT）丢进去，它自动切分、建索引，之后用自然语言提问即可。

典型问题：

- 「实验室危险化学品怎么领用？需要谁审批？」→ 回答引用《实验室安全管理办法》第 X 条，点开看原文页
- 「出差住宿费报销标准是多少？」→ 从经费报销细则的表格里找到标准，并列出原文
- 「新版的请假规定和旧版有什么变化？」→ 识别制度版本，以现行有效版本为准
- 「奖学金怎么申请？」（口语化、模糊）→ 自动改写为书面问法再检索

核心能力：

| 能力 | 说明 |
|---|---|
| 上传即入库 | 拖入文件 → 自动解析 → 切条款 → 建索引 → 发布，全程可视化进度 |
| 混合检索 | 向量检索（BGE）+ 关键词检索（BM25）+ 文号/条款号精确命中，三路融合 |
| 引用溯源 | 每个结论都标 `[证据n]`，可跳转到 PDF 物理页查看原文 |
| 多轮追问 | 「那第十二条呢？」能结合上一轮补全主体 |
| 时效治理 | 登记生效/废止日期，新旧版本冲突时以现行版本为准并提示 |
| 更新即时生效 | 新制度发布后自动切换，无需重启服务 |
| 不知道就直说 | 知识库里没有依据时明确拒答，不编造条款号 |

适合场景：学校/院系的制度问答、办事大厅导办、企业内部规章问答。

---

## 2. 它是怎么工作的

```
                    ┌──────────────────────────────┐
   上传 PDF/DOCX ──▶ │ 解析 → 条款切分 → 质量检查   │
   MD/TXT           │ → 元数据登记 → 增量向量化     │
                    └──────────────┬───────────────┘
                                   ▼
                          发布 release（原子切换）
                                   ▼
   提问 ──▶ 意图路由 ──▶ 多轮改写 ──▶ 混合检索（BGE+BM25+条款号）
                                   ▼
                          时效/适用范围过滤 → 证据组装
                                   ▼
                     提示词（强制引用、无依据拒答）→ 大模型流式生成
                                   ▼
                     回答 + [证据n] + 原文页链接
```

技术组件：

| 层 | 选型 | 说明 |
|---|---|---|
| 向量模型 | `BAAI/bge-base-zh-v1.5`（768 维） | 本地运行，约 400MB，CPU 也能跑 |
| 向量库 | Chroma（默认）/ npz（回退） | Chroma 纯本地持久化，不需要 Docker |
| 关键词检索 | jieba + 自建倒排 BM25 | 中文分词后做 BM25 打分 |
| 元数据 | SQLite | 文档登记、任务、发布记录 |
| 大模型 | 任意 OpenAI 兼容接口 | 通过 `.env` 配置 base_url / key / model |
| 服务端 | FastAPI + uvicorn | 提供 HTTP 与流式问答 |
| 前端 | Vue 3 + Vite | 对话、引用、PDF 预览、上传 |

> 换大模型不用改代码：任何兼容 `POST /v1/chat/completions` 的服务都能接（DeepSeek、通义、vLLM、Ollama、LM Studio 等）。

---

## 3. 目录结构

```
PolicyRAG/
├─ start.py              # 一键启动（start/status/stop/restart/install）
├─ start.bat             # Windows 双击启动
├─ app/                  # 后端主程序
│  ├─ main.py            # FastAPI 入口
│  ├─ config.py          # 配置（路径、模型、端口）
│  ├─ core/              # 发布指针、运行时单例
│  ├─ api/               # HTTP 接口：问答/文档/入库/信息
│  ├─ ingest/            # 解析、条款切分、质量检查、发布
│  ├─ retrieval/         # 编码器、BM25、向量后端、混合检索
│  ├─ llm/               # 大模型客户端、提示词、路由改写
│  └─ store/             # SQLite 元数据
├─ web/                  # Vue 前端（npm run build 生成 dist）
├─ knowledge/            # 样例制度文档与元数据（小文件，可入库 Git）
├─ scripts/              # 命令行工具：入库、建索引、检查、评测
├─ tests/                # 单元测试与评测集
├─ docs/                 # 架构与接口约定
├─ models/               # 向量模型（不入库）
└─ data/                 # 运行数据（不入库）
   ├─ docs/              # 每份文档的解析结果
   ├─ releases/          # 发布的索引版本
   ├─ vector_store/      # Chroma 数据
   └─ state/             # active_release.json、catalog.sqlite
```

**只有 `app/`、`web/`、`knowledge/`、`scripts/`、`tests/`、`docs/` 会进 Git**；模型、索引、数据库、日志都不进。

---

## 4. 快速开始（15 分钟）

### 0. 你需要先装好

| 软件 | 版本 | 检查命令 | 说明 |
|---|---|---|---|
| Python | 3.10 及以上 | `python --version` | 后端运行环境 |
| Git | 任意较新版本 | `git --version` | 拉代码 |
| Node.js | 20 / 22 | `node --version` | 只有改前端才需要 |

Windows 上如果 `python` 命令不可用，把下文所有 `python` 换成 `py`。

### 1. 获取代码

```bash
git clone <仓库地址>
cd PolicyRAG
```

> 没有仓库访问权限？找维护者把你的 GitHub 账号加进私有仓库 Collaborators（见第 8 节）。

### 2. 创建虚拟环境并安装依赖

虚拟环境就是「这个项目专用的 Python 包目录」，避免和电脑上其他项目打架。

```bash
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1
# Windows CMD
.\.venv\Scripts\activate.bat
# macOS / Linux
source .venv/bin/activate
```

如果 PowerShell 提示「禁止运行脚本」，先执行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

然后安装依赖（国内网络务必加镜像）：

```bash
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**要用显卡加速向量编码**（可选，CPU 也能跑）：

```bash
# NVIDIA 显卡 + CUDA 12.8
python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
```

验证：

```bash
python -c "import fastapi, chromadb, fitz, jieba; print('依赖安装成功')"
```

### 3. 准备向量模型

项目需要 `BAAI/bge-base-zh-v1.5`，放在 `models/bge-base-zh-v1.5/` 下。

**方式 A：向维护者要**（最快，把整个文件夹拷进 `models/` 即可）。

**方式 B：自己下载**：

```bash
python -m pip install huggingface_hub
# 设置国内镜像（可选）
set HF_ENDPOINT=https://hf-mirror.com
python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-base-zh-v1.5', local_dir='models/bge-base-zh-v1.5')"
```

验证：`models/bge-base-zh-v1.5/` 下应有 `pytorch_model.bin`（约 400MB）、`config.json`、`tokenizer.json` 等文件。

### 4. 配置大模型

复制配置文件：

```bash
copy .env.example .env     # Windows
cp .env.example .env       # macOS / Linux
```

用记事本打开 `.env`，填写你的模型服务：

```ini
POLICYRAG_LLM_BASE_URL=https://api.example.com/v1
POLICYRAG_LLM_API_KEY=sk-你的密钥
POLICYRAG_LLM_MODEL=模型名称
POLICYRAG_LLM_THINKING=disabled
```

- 正式使用填外部 API；当前项目联调使用的是火山方舟：

  ```ini
  POLICYRAG_LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/coding/v3
  POLICYRAG_LLM_API_KEY=ark-你的密钥
  POLICYRAG_LLM_MODEL=deepseek-v4.1-flash
  ```

  注意方舟的 `/api/v3` 与 `/api/coding/v3` 是两个入口，密钥和可用模型可能不同；换服务商时只改这三行，不用改代码。
- `POLICYRAG_LLM_THINKING=disabled` 会关闭推理型模型的深度思考（方舟用 `thinking: {"type":"disabled"}`）；如果服务商不认这个参数，改成 `none`。
- 想完全本地跑，也可以接 Ollama：`POLICYRAG_LLM_BASE_URL=http://127.0.0.1:11434/v1`，模型名填 `qwen2.5:7b` 之类。
- `.env` 已被 Git 忽略，**不会**泄露密钥；不要把密钥写进代码。

### 5. 灌入样例知识库

仓库自带 5 份样例制度文件，先跑通流程：

```bash
python scripts/ingest.py knowledge/samples --publish
```

如果提示找不到命令，先确认虚拟环境已激活（命令行前面有 `(.venv)`）。

成功标志：

- 终端打印每份文档的单元数、发布版本号；
- `data/state/active_release.json` 文件出现；
- `data/releases/` 下出现一个 `rel-...` 目录。

### 6. 启动服务

```bash
python start.py
```

脚本会自动检查虚拟环境、模型、`.env` 和前端，在后台启动服务，等服务就绪后打开浏览器。**Windows 上也可以直接双击 `start.bat`。**

首次在新机器上使用，先装依赖：

```bash
python start.py install
```

其他常用命令：

```bash
python start.py status     # 查看运行状态、知识库版本、模型是否正常
python start.py stop       # 停止服务
python start.py restart    # 重启
```

不想用脚本时，也可以手动启动（这个窗口不要关，关掉服务就停了）：

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 7880
```

### 7. 打开网页

用 `start.py` 启动会自动打开浏览器；没打开就手动访问 **http://127.0.0.1:7880** 。

试问：`实验室危险化学品怎么领用？`

成功标志：

- 回答逐字出现；
- 回答里带 `[证据1]`、`[证据2]` 之类的角标；
- 右侧「参考来源」列出文件名和页码，点击能看到 PDF 原页。

### 8. 改前端（仅前端开发需要）

```bash
cd web
npm install          # 第一次
npm run dev          # 开发模式，浏览器打开提示的地址
npm run build        # 构建到 web/dist，后端会自动托管
```

---

## 5. 日常使用

### 5.1 上传一份新制度

**方式 A：网页上传（推荐）**

1. 点左下角「知识库」→「上传资料」；
2. 拖入 PDF / DOCX / MD / TXT；
3. 填写元数据（可选但强烈建议）：制度名称、文号、生效日期、归口部门；
4. 等待任务从「解析 → 切分 → 建索引 → 发布」完成；
5. 发布后立即生效，**不需要重启服务**。

**方式 B：命令行**

```bash
# 单份
python scripts/ingest.py "D:/制度文件/实验室安全管理办法.pdf"

# 整个目录
python scripts/ingest.py knowledge/samples --publish

# 带元数据
python scripts/ingest.py 文件.pdf --meta knowledge/samples/metadata.json
```

### 5.2 元数据怎么填

元数据决定「时效治理」是否准确，建议每份制度都填：

| 字段 | 示例 | 说明 |
|---|---|---|
| `title` | 实验室安全管理办法 | 制度全称 |
| `doc_no` | 校发〔2024〕12号 | 文号，可空 |
| `version_label` | 2024修订版 | 版本说明 |
| `effective_from` | 2024-07-01 | 生效日期 |
| `effective_to` | 空 | 废止日期，现行制度留空 |
| `department` | 实验室与设备管理处 | 归口部门 |
| `status` | active / superseded / repealed | 现行 / 被替代 / 废止 |
| `supersedes` | 旧版 doc_id | 本文件替代了哪份 |

### 5.3 更新制度

1. 上传新版文件，元数据里把旧版 `status` 改成 `superseded`，并填写新版的 `supersedes`；
2. 发布后，同一问题的回答会切到新版，并在回答前提示「《XX办法》2025修订版自 2025-09-01 起施行」；
3. 旧版内容不会删除，但不再作为首选依据。

### 5.4 删除误传资料

网页「知识库」→ 对应任务 →「删除本批新增资料」。只会删除该批次新增的内容，不影响已有制度。

---

## 6. 常见问题

### Q1：`pip install` 卡住 / SSL 报错

用国内镜像：

```bash
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

仍然失败时换阿里云镜像：`-i https://mirrors.aliyun.com/pypi/simple`。

### Q2：模型加载失败 / `No such file or directory: models/bge-base-zh-v1.5`

模型没放对位置。确认 `models/bge-base-zh-v1.5/pytorch_model.bin` 存在。没有就按第 4 节第 3 步下载。

### Q3：`CUDA out of memory` 或没有显卡

把 `.env` 改成：

```ini
POLICYRAG_DEVICE=cpu
```

BGE-base 在 CPU 上编码几千条也就一两分钟。

### Q4：端口 7880 被占用

先看是不是 PolicyRAG 自己已经启动：`python start.py status`。确认被其他程序占用时，改 `.env` 里的 `POLICYRAG_PORT=7881` 再 `python start.py`，或用 `netstat -ano | findstr :7880` 查占用进程。

### Q5：打开网页一片空白

前端没有构建。执行：

```bash
cd web
npm install
npm run build
```

然后刷新页面。

### Q6：回答「生成模型不可用」

`.env` 里的 `POLICYRAG_LLM_BASE_URL` / `API_KEY` / `MODEL` 不对，或模型服务没启动。用下面的命令自测：

```bash
curl -X POST "%POLICYRAG_LLM_BASE_URL%/chat/completions" -H "Authorization: Bearer %POLICYRAG_LLM_API_KEY%" -H "Content-Type: application/json" -d "{\"model\":\"%POLICYRAG_LLM_MODEL%\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

### Q7：回答里没有引用 / 引用点不开

- 没有引用：说明检索没找到依据，这是正常的拒答；
- 引用点不开：确认该文档是 PDF（DOCX 没有原页图，只能看证据文本）。

### Q8：Chroma 报错

临时切回零依赖后端：

```ini
POLICYRAG_VECTOR_BACKEND=npz
```

然后重新发布一次（`python scripts/build_index.py`）。

### Q9：中文变乱码

Windows 终端执行 `chcp 65001`，或在 PowerShell 里设置 `$env:PYTHONIOENCODING="utf-8"`。

### Q10：怎么确认知识库当前版本

浏览器访问 `http://127.0.0.1:7880/api/info`，看 `release_id`、`doc_count`、`unit_count`、`model_ready`。

---

## 7. 开发指南

### 7.1 代码入口

| 想改什么 | 看哪里 |
|---|---|
| 问答流程、SSE 事件 | `app/api/ask.py` |
| 提示词（引用规则、拒答） | `app/llm/prompts.py` |
| 意图路由、多轮改写 | `app/llm/routing.py` |
| 检索融合、证据预算 | `app/retrieval/engine.py` |
| 条款切分规则 | `app/ingest/structure.py` |
| 文档解析 | `app/ingest/parser.py` |
| 发布与热更新 | `app/ingest/publish.py`、`app/core/release.py` |
| 前端页面 | `web/src/App.vue` |

接口与数据格式的**唯一权威**是 [docs/架构与接口.md](docs/架构与接口.md)，改动接口前先更新它。

### 7.2 跑测试

```bash
# 单元测试（不依赖模型、网络、大模型）
python -m pytest tests -q

# 端到端冒烟：服务启动后，检查检索/引用/拒答/多轮/PDF 页图
python scripts/smoke_test.py --base-url http://127.0.0.1:7880

# 验收：新制度零准备问答 + 上传新版后即时生效（会自己造两份临时 DOCX）
python scripts/acceptance_update_test.py --base-url http://127.0.0.1:7880

# 多轮对话测试：7 轮连续追问，打印每轮改写后的问句、回答与引用
python scripts/chat_test.py --base-url http://127.0.0.1:7880
```

### 7.3 用 curl 调试接口

```bash
# 服务信息
curl http://127.0.0.1:7880/api/info

# 提问（流式，逐行 JSON）
curl -N -X POST http://127.0.0.1:7880/api/ask ^
  -H "Content-Type: application/json" ^
  -d "{\"question\":\"实验室危化品怎么领用？\",\"history\":[]}"

# 文档列表
curl http://127.0.0.1:7880/api/documents
```

### 7.4 新增一种文档格式

1. 在 `app/ingest/parser.py` 增加解析分支，返回统一的 `pages` 结构；
2. 在 `app/api/ingest.py` 的扩展名白名单里加上后缀；
3. 在 `tests/test_ingest.py` 补一个用例。

### 7.5 评测

```bash
# 服务启动后跑全部 40 题
python scripts/evaluate.py --base-url http://127.0.0.1:7880

# 只跑前 5 题 / 指定题号 / 低于 80% 通过率返回错误码
python scripts/evaluate.py --limit 5
python scripts/evaluate.py --id q01 --id q22
python scripts/evaluate.py --fail-under 0.8
```

输出每题的引用命中、拒答是否正确、耗时，以及总体准确率，结果写入 `tests/eval/results-<时间>.json`。

### 7.6 没有大模型时怎么联调

项目自带一个假模型服务，接口与 OpenAI 一致，返回固定回答与 `[证据1]`：

```bash
# 另开一个终端
python scripts/mock_llm.py --port 8081

# .env 里指向它
POLICYRAG_LLM_BASE_URL=http://127.0.0.1:8081/v1
POLICYRAG_LLM_MODEL=mock-llm
```

它只用于验证「上传 → 检索 → 流式回答 → 引用」链路是否通，不产生真实回答。

---

## 8. Git 协作

### 8.1 拿到仓库权限

私有仓库需要维护者在 GitHub 上操作：

1. 打开仓库 → **Settings** → **Collaborators** → **Add people**；
2. 输入你的 GitHub 用户名，选择 **Write** 权限；
3. 你会在邮箱里收到邀请，点接受后即可 clone / push。

### 8.2 日常流程

```bash
git pull                      # 每次开始前先拉最新代码
git checkout -b feature/xxx   # 新功能开分支
# ... 改代码 ...
git add -A
git commit -m "feat: 增加报销标准表格解析"
git push -u origin feature/xxx
```

然后在 GitHub 上发起 Pull Request，由维护者合并。**不要直接往 main 推。**

### 8.3 哪些东西不要提交

`.gitignore` 已排除：`.venv/`、`node_modules/`、`models/`、`data/`、`*.npz`、`*.sqlite`、`.env`。如果 `git status` 里看到它们，说明 `.gitignore` 被改坏了，先别提交。

### 8.4 提交信息建议

```
feat: 新功能
fix: 修复问题
docs: 文档
refactor: 重构（不改行为）
test: 测试
```

---

## 9. 已知限制与后续计划

当前版本（0.1）的边界：

- **扫描件 PDF 不支持 OCR**：纯图片 PDF 解析出来是空的，需要先做 OCR（后续接 OCR 服务）；
- **DOCX 没有页图**：只能看证据文本，没有 PDF 原页预览；
- **表格理解有限**：复杂合并单元格可能切分不理想；
- **时效治理依赖人工元数据**：系统不会自动判断「哪份是新版」，需要在元数据里标注；
- **单机单进程**：没有做多用户权限与并发压测；
- **本地 27B 量化模型较慢**：单题约 40–70 秒（12GB 显卡混合推理）；接外部 API 后会明显变快，检索本身是亚秒级。

本地实测记录（119 项单测、13 项冒烟、12 项验收、40 题评测 39/40）见 [docs/验收记录.md](docs/验收记录.md)。

后续计划：

- [ ] 扫描件 OCR 接入
- [ ] 制度版本关系自动推断（文号/名称相似度）
- [ ] 部门级权限
- [ ] 问答质量自动评测报表
- [ ] 更多文档格式（Excel、网页）

---

## 更新记录

### 0.2.0

- 架构：配置对象化（`app/settings.py`）、领域包（`app/domain/versions.py`）、发布存储（`app/store/releases.py`）、
  部件契约（`app/core/contracts.py`）、组合根（`app/wiring.py`）；检索/入库/问答各自的流水线拆开，
  详见 [docs/架构.md](docs/架构.md) 与 [docs/复用指南.md](docs/复用指南.md)。
- 检索：新增查询拆解（`app/retrieval/planner.py`），「A 和 B 分别怎么规定」会拆成多个子句分别召回，
  并为每个子问题保留至少一个证据位；40 题评测的跨文档题 5/5。
- 入库：`/api/ingest` 的 `metadata` 支持与 `files` 顺序一致的对象列表，一次上传多份文件可各自登记标题/文号/生效日期。
- 质量：`scripts/drill_extract.py` 抽包演练（`DRILL OK`）与 `tests/test_architecture.py` 架构守卫，
  保证这套内核可以被单独拿走复用。

## 附：一句话排障顺序

```
网页打不开 → 服务起了吗（终端有没有 Uvicorn running）
服务起了但白屏 → 前端 build 了吗（web/dist 存在吗）
能问但报模型不可用 → .env 的 LLM 三项对吗，模型服务通吗
回答没引用 → 先看 /api/info 的 doc_count 是不是 0（知识库没灌）
引用点不开 → 文档是 PDF 吗
```

有问题先看终端日志；仍然解决不了，把「你执行的命令 + 完整报错 + /api/info 输出」发给维护者。
