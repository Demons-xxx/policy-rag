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
10. [在新工作区从零跑起来（迁移清单）](#10-在新工作区从零跑起来迁移清单)

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
| 引用溯源 | 每个结论都标 `[证据n]`，可跳转到原页查看：PDF 原件，或 Word 转出的页图 |
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

| 层 | 选型 | 位置 / 体积 | 说明 |
|---|---|---|---|
| 向量模型 | `BAAI/bge-base-zh-v1.5`（768 维） | `models/bge-base-zh-v1.5/`，约 391 MB | 本地运行，**必需**；CPU 也能跑 |
| 重排模型 | `BAAI/bge-reranker-v2-m3`（Cross-Encoder） | `models/bge-reranker-v2-m3/`，约 2.2 GB | 建议安装；缺失自动关闭重排 |
| 向量库 | Chroma（默认）/ npz（回退） | `data/vector_store/` 或 release 内 `vectors.npz` | Chroma 纯本地持久化，不需要 Docker |
| 关键词检索 | jieba + 自建倒排 BM25 | 无模型文件 | 中文分词后做 BM25 打分 |
| 元数据 | SQLite | `data/state/catalog.sqlite` | 文档登记、任务、发布记录 |
| 大模型 | 任意 OpenAI 兼容接口 | `.env` 配置 | 必需；没有它只能检索不能生成 |
| 扫描件 OCR | 本地 RapidOCR（ONNX，CPU）+ 可选外部 HTTP 端点 | `models/rapidocr/`（约 16 MB）；`POLICYRAG_OCR_ENDPOINT` 优先 | 默认本地，无需外部服务 |
| PDF 版式解析 | docling（外部解释器子进程） | 外部运行时 + `models/docling/` 产物（已拷贝，约 367 MB） | 可选；本机默认 docling，可单次切回 pymupdf |
| DOCX 页图 | 本机 Microsoft Word（COM） | 系统安装 | 可选；没有 Word 就跳过 |
| 服务端 / 前端 | FastAPI + uvicorn / Vue 3 + Vite | `web/dist/` | 前端构建产物不进 Git |

> 换大模型不用改代码：任何兼容 `POST /v1/chat/completions` 的服务都能接（DeepSeek、通义、vLLM、Ollama、LM Studio 等）。

### 2.1 模型与服务清单（配置项对照）

| 组件 | 默认位置 / 地址 | 关键配置项 | 不可用时的行为 |
|---|---|---|---|
| BGE 向量模型 | `models/bge-base-zh-v1.5` | `POLICYRAG_EMBEDDING_DIR`、`POLICYRAG_DEVICE` | 第一次检索/入库时报错；`/api/info` 的 `error` 字段会带原因 |
| 重排模型 | `models/bge-reranker-v2-m3` | `POLICYRAG_RERANK=0/1`、`POLICYRAG_RERANK_MODEL_DIR`、`_TOP_N`、`_BATCH` | 模型缺失或加载失败：记一条 warning，回退到融合原排序，问答不受影响 |
| 生成模型 | `.env` 的 `POLICYRAG_LLM_BASE_URL` | `..._MODEL`、`..._API_KEY`、`..._THINKING`、`RAG_LLM_RETRIES`、`RAG_LLM_FIRST_TOKEN_TIMEOUT` | `/api/info` 的 `model_ready=false`，问答返回「生成模型不可用」；重试策略见第 2.1.6 节 |
| 扫描件 OCR | 本地 RapidOCR（`models/rapidocr`，默认开）；配置 `POLICYRAG_OCR_ENDPOINT` 则优先外部端点 | `POLICYRAG_OCR_LOCAL`、`_MODEL_DIR`、`_DPI`、`_MIN_CHARS`、`_TIMEOUT`、`_MAX_PAGES` | 本地引擎缺包/权重且无端点：扫描页保留原样并给「疑似扫描件」提示；端点/本地 OCR 失败：入库任务失败并给出「第 N 页 OCR 失败：原因」 |
| docling 解析 | 模型 `models/docling/`；解释器默认同盘 `../RAG/local_service/runtime/parse/Scripts/python.exe` | `POLICYRAG_PARSER_PDF=pymupdf/docling`、`POLICYRAG_DOCLING_PYTHON`、`_ARTIFACTS`、`_DEVICE`、`_TIMEOUT` | 解释器不存在时入库报中文错误，提示设置 `POLICYRAG_DOCLING_PYTHON`；选 pymupdf 时完全不受影响 |
| Word 页图 | 本机 Word 的 COM 接口 | `POLICYRAG_DOCX_PREVIEW=1/0`、`POLICYRAG_DOCX_PREVIEW_TIMEOUT` | 无 Word / 转换失败：DOCX 文本照常入库，只记一条 `docx_preview` 提示，`has_pdf=0` |
| 向量库 | `POLICYRAG_VECTOR_BACKEND=chroma` 或 `npz` | — | chroma 不可用时可选 `npz`（零外部依赖，但全量加载进内存） |

#### 2.1.1 BGE 向量模型（必需）

- 模型：`BAAI/bge-base-zh-v1.5`，768 维中文检索模型，负责入库切片段后的向量化，以及提问时的查询向量。
- 位置：默认 `models/bge-base-zh-v1.5/`；也可以放仓库外，用 `POLICYRAG_EMBEDDING_DIR` 指绝对路径（推荐多项目共用一个模型目录）。
- 设备：`POLICYRAG_DEVICE=auto`（有 CUDA 用 CUDA，否则 CPU）；查询时会自动加 BGE 的检索前缀。
- 换模型注意：换成不同维度的模型后，必须**重新发布**（旧 release 里的向量不可混用）。

#### 2.1.2 重排模型（建议）

- 模型：`BAAI/bge-reranker-v2-m3`，Cross-Encoder，对 RRF 融合后的前 `POLICYRAG_RERANK_TOP_N`（默认 20）个候选重打分；分数写入检索结果的 `scores.rerank`。
- 设备：跟随 `POLICYRAG_DEVICE`；CUDA 下用 fp16，显存不足可设 `POLICYRAG_DEVICE=cpu`（慢一些但能用）。
- 关闭：`POLICYRAG_RERANK=0`。关闭或模型缺失时系统仍可用，只是排序质量下降。
- 与编码共用 GPU 并发信号量（`RAG_GPU_CONCURRENCY`，默认 2）：两张模型常驻显存，但同一时刻只有有限个推理在跑，避免并发请求把显存打满。

#### 2.1.3 扫描件 OCR（默认本地 RapidOCR，可选外部端点）

PDF 每页先走文本层（PyMuPDF 或 docling），文本数 < `POLICYRAG_OCR_MIN_CHARS`（默认 20）的页渲染成 PNG，再按优先级选 OCR：

1. **外部端点**（配置了 `POLICYRAG_OCR_ENDPOINT` 时）：`multipart/form-data` 字段 `image`，返回 `{"code": 0, "result": "markdown"}`；契约与同盘 RAG 项目的 `remote_ocr.py` 一致，可直接复用同一个服务。
2. **本地 RapidOCR**（`POLICYRAG_OCR_LOCAL=1`，默认）：ONNX Runtime、CPU 推理，权重随项目放在 `models/rapidocr/`（3 个 onnx 共约 16 MB）；一页约 2–6 s，输出按阅读顺序的文本行，质量检查与外部端点相同。首次调用加载引擎约 1 s，进程内复用。

- 两条路线都失败/都关闭时：能入库，扫描页保留空文本并在问题清单里提示「疑似扫描件」，问答不会引用这些页。
- **能用大模型 API 当 OCR 吗？** 不能。纯文本模型（如 DeepSeek）没有图像输入；多模态模型虽然能“看图”，但整页 OCR 会漏字/编造、没有坐标与表格结构、按页计费成本高，只适合对 OCR 结果做后处理（校对、结构化），不适合替代 OCR 引擎。本项目不把 LLM 当 OCR 用。
- 配置：`POLICYRAG_OCR_LOCAL=1/0`、`POLICYRAG_OCR_MODEL_DIR`（默认 `models/rapidocr`）、`POLICYRAG_OCR_ENDPOINT`、`POLICYRAG_OCR_DPI=200`、`POLICYRAG_OCR_MIN_CHARS=20`、`POLICYRAG_OCR_TIMEOUT=180`、`POLICYRAG_OCR_MAX_PAGES=100`。
- 自测端点（任意一页 PNG）：
  ```bash
  curl -X POST http://127.0.0.1:8000/ocr -F "image=@page.png"
  ```

#### 2.1.4 PDF 解析：pymupdf 还是 docling（可自选）

两种解析器，按需选择：

| 解析器 | 特点 | 适合 |
| --- | --- | --- |
| `pymupdf`（默认） | 快（毫秒级/页），只取文本层 | 普通制度/文档 PDF，时间敏感 |
| `docling` | 慢（3 页约 4–9s，首次加载模型更久），带版式与表格还原，表格切 `table_row` | 表格多、版式复杂、扫描/双栏，时间宽裕时质量更好 |

**怎么选（三选一，优先级从高到低）**：

1. **单次入库**：`python scripts/ingest.py 路径 --parser docling|pymupdf`；
2. **接口上传**：`metadata` 里加 `"parser": "docling"`（对象=全部文件共用，数组=逐文件），`POST /api/ingest` 的任务按它解析；
3. **全局默认**：`.env` 的 `POLICYRAG_PARSER_PDF=pymupdf|docling`（改完重启服务）。

> 本项目**本机部署已设 `POLICYRAG_PARSER_PDF=docling`**（时间充裕、优先质量）；换机器或追求速度时改成 `pymupdf` 即可。文档解析器记录在 `meta.json` 的 `parser` 字段，事后可追溯。

- 权重位置：`models/docling/`（已随项目拷贝，约 367 MB；`POLICYRAG_DOCLING_ARTIFACTS` 可覆盖）。
- 运行时：主环境不装 docling，`scripts/docling_worker.py` 由外部「已装 docling 的 Python」执行；默认同盘 `../RAG/local_service/runtime/parse/Scripts/python.exe`（Linux 为 `../RAG/script/runtime/parse/bin/python`）。没有同盘项目时必须显式配置：
  ```ini
  POLICYRAG_PARSER_PDF=docling
  POLICYRAG_DOCLING_PYTHON=D:/path/to/docling-venv/Scripts/python.exe
  POLICYRAG_DOCLING_ARTIFACTS=D:/path/to/docling-model
  POLICYRAG_DOCLING_DEVICE=cuda
  POLICYRAG_DOCLING_TIMEOUT=600
  ```
- docling 自带的是「可插拔 OCR 引擎」的接入能力（本机环境装有 easyocr），不是单独一个内置模型；本项目为了让 docling 专注版式/表格，在 worker 里 `do_ocr=False`，扫描页仍由 2.1.3 的外部 OCR 端点处理。
- 手工验证（用带 docling 的解释器跑 worker）：
  ```bash
  D:/path/to/docling-venv/Scripts/python.exe scripts/docling_worker.py --input 样例.pdf --output out.json --device cuda --artifacts models/docling
  ```

#### 2.1.5 Word 页图（可选，仅 Windows）

- DOCX 没有「页」的概念，为了在前端预览原文页，入库时用本机 Word 的 COM 把 DOCX 另存为 `source.pdf`（`ExportAsFixedFormat`），再由 PyMuPDF 渲染页图。
- 开关：`POLICYRAG_DOCX_PREVIEW=1`；超时 `POLICYRAG_DOCX_PREVIEW_TIMEOUT=120`。
- 没装 Word：DOCX 文本照常入库，`has_pdf=0`，前端只显示文本证据；可稍后在装了 Word 的机器上运行 `python scripts/docx_preview.py` 补页图。

#### 2.1.6 生成模型（必需）

- 任何 OpenAI 兼容 `/chat/completions` 服务；`POLICYRAG_LLM_THINKING=disabled` 会发 `thinking: {"type":"disabled"}` 关掉推理型模型的思考，服务不认这个字段就改成 `none`。
- 客户端自带韧性：连接错误/5xx/429/首 token 超时按 `RAG_LLM_RETRIES`（默认 2）指数退避重试；已经输出 token 后中断只报「回答未完成」，不会重复内容。
- 零模型联调：`python scripts/mock_llm.py --port 8081`，`.env` 指 `http://127.0.0.1:8081/v1` 即可验证「检索 → 证据 → 流式输出 → 引用」全链路。

#### 2.1.7 向量库（二选一）

- `chroma`（默认）：发布时把片段向量写入 `data/vector_store/`，集合名 `policy_<release_id>`；适合文档量大、想减少内存占用。
- `npz`：向量存在 release 目录的 `vectors.npz`，单机全量加载，零外部依赖；适合小库/离线环境。
- 切换后端后需要**重新发布**（或至少发布新 release），旧 release 的向量存储格式不同。

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

验证：`models/bge-base-zh-v1.5/` 下应有 `pytorch_model.bin`（约 391MB）、`config.json`、`tokenizer.json` 等文件。

> 重排模型、OCR、docling、Word 页图都是**可选项**，不装只影响对应能力，不阻塞问答；各自的模型与接入方式见 [2.1 模型与服务清单](#21-模型与服务清单配置项对照)。

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
- `.env` 里其余可选项（重排、OCR、docling、DOCX 页图、缓存、并发、端口）见 [2.1](#21-模型与服务清单配置项对照) 与 [10.4](#104-配置-env)。

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

**并发与 workers 建议**：默认单进程就够用。编码与重排共享一个 GPU 信号量
（`RAG_GPU_CONCURRENCY`，默认 2），超出的请求排队等待、不会同时把显存打满，
4 个并发问答实测正常。显存较小（≤8GB 且同时加载 BGE 与重排模型）时设为 1 更稳；
不建议开多个 uvicorn worker：每个 worker 会各加载一份模型、各占一份显存，
反而更容易 OOM。要横向扩容，优先把 `RAG_VECTOR_BACKEND` 换成 chroma + 独立向量服务。

### 7. 打开网页

`start.py` 默认**不自动打开浏览器**（避免反复弹窗口），启动后手动访问 **http://127.0.0.1:7880** ；
想让它自动打开就加 `--open`：`python start.py start --open`。

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

### 5.0 回答与对话操作

- **重新生成**：回答下方 ↻，用同一段上下文（该轮之前的历史）重答；
- **导出 Markdown**：回答下方 ⤓，导出文件含问题、回答与参考来源（文号/条款/页码）；
- **重命名/删除对话**：左侧「最近对话」悬停后出现铅笔/垃圾桶图标；
- **资料库筛选**：资料库顶部可按状态（现行/已被替代/已废止）与归口部门筛选；
  点开一份文档，右侧面板显示生效期、文号版本、版本链，并可**停用/恢复**该文档（不触发重新发布，状态立即生效）。

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
- 引用点不开：确认该文档是 PDF，或 DOCX 且服务器装有 Word（`POLICYRAG_DOCX_PREVIEW=1`）；否则 DOCX 只能看证据文本。

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

汇总里除了通过率，还有三项质量指标：**证据召回**（来源是否命中预期文档 + 条款）、
**引用准确率**（答案里的 `[证据n]` 是否都存在对应来源，越界/悬空算错）、
**拒答冲突数**（同一回答既写拒答句又带引用的次数）。改检索或提示词后重点看这三项，
它们比通过率更早暴露「检索对了但证据选错」的问题。

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

- **扫描件需要 OCR 端点**：配置 `POLICYRAG_OCR_ENDPOINT` 后自动识别文本层不足的页；
  未配置时仍是空页提示，需先做 OCR 或改用 docling 后端；
- **DOCX 页图依赖 Word**：服务器没有 Word 或关闭 `POLICYRAG_DOCX_PREVIEW` 时，DOCX 只能看证据文本；
  已入库的历史文档可运行 `python scripts/docx_preview.py` 补页图；
- **PDF 表格**：默认 PyMuPDF 只取文本层；需要版式/表格更强时可切 `POLICYRAG_PARSER_PDF=docling`（外部 docling 解释器，较慢，适合离线批量入库）；
- **表格理解有限**：复杂合并单元格可能切分不理想；
- **时效治理依赖人工元数据**：系统不会自动判断「哪份是新版」，需要在元数据里标注；
- **单机单进程**：没有多用户权限体系；并发只做了 4 请求的显存保护验证，没有做大规模压测；
- **本地 27B 量化模型较慢**：单题约 40–70 秒（12GB 显卡混合推理）；接外部 API 后会明显变快，检索本身是亚秒级。

本地实测记录（119 项单测、13 项冒烟、12 项验收、40 题评测 39/40）见 [docs/验收记录.md](docs/验收记录.md)。

后续计划：

- [x] 扫描件 OCR 接入（OCR 端点 + docling 可选后端）
- [ ] 制度版本关系自动推断（文号/名称相似度）
- [ ] 部门级权限
- [ ] 问答质量自动评测报表
- [ ] 更多文档格式（Excel、网页）

---

## 10. 在新工作区从零跑起来（迁移清单）

这一节是给「把代码拉到另一台机器 / 另一个目录」准备的。仓库**不含**虚拟环境、模型、
运行数据、前端构建产物和 `.env`，所以下面这些要单独准备。

### 10.1 先看清哪些要准备

| 项 | 是否必需 | 说明 |
|---|---|---|
| Python 3.10+ | 必需 | 后端运行环境；建议 3.10/3.11 |
| Git | 必需 | 拉代码 |
| BGE 向量模型（391 MB） | 必需 | 放 `models/bge-base-zh-v1.5/` 或配置外置路径 |
| 生成模型（OpenAI 兼容） | 必需 | 外部 API 或本地服务，填 `.env` |
| 重排模型（2.2 GB） | 建议 | 缺失自动关闭重排，质量略降 |
| 模型/数据目录写权限 | 必需 | `models/`、`data/`、`log/` 要可写 |
| Node.js 20/22 | 改前端才需要 | 构建 `web/dist` |
| Microsoft Word | 可选 | 仅 DOCX 页图预览 |
| docling 外部解释器 | 可选 | 仅 `POLICYRAG_PARSER_PDF=docling` 时需要 |
| OCR 服务端点 | 可选 | 仅扫描件 PDF 需要 |
| NVIDIA GPU + CUDA | 可选 | CPU 也能跑，只是编码/重排慢 |

### 10.2 拉代码 + 安装依赖

```bash
git clone <仓库地址>
cd PolicyRAG
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

要用显卡（推荐）：

```bash
.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu128
```

验证：

```bash
.venv\Scripts\python.exe -c "import fastapi, chromadb, fitz, jieba, torch; print('ok', torch.cuda.is_available())"
```

### 10.3 模型：下载或直接拷贝

两个模型都可以从旧机器整目录拷贝（最快），也可以按下面命令下载：

```bash
.venv\Scripts\python.exe -m pip install -U huggingface_hub
# 国内网络可加镜像
set HF_ENDPOINT=https://hf-mirror.com            # PowerShell: $env:HF_ENDPOINT="https://hf-mirror.com"
.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-base-zh-v1.5', local_dir='models/bge-base-zh-v1.5')"
.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-reranker-v2-m3', local_dir='models/bge-reranker-v2-m3')"
```

- 找不到 `pytorch_model.bin`/`model.safetensors` 就说明没下完；`models/` 已被 Git 忽略，不会误提交。
- 模型不必放在仓库里：用 `POLICYRAG_EMBEDDING_DIR`、`POLICYRAG_RERANK_MODEL_DIR` 指绝对路径即可（多项目共用一份最省磁盘）。

### 10.4 配置 `.env`

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

最小可用配置（生成模型 + 默认本地模型目录）：

```ini
POLICYRAG_LLM_BASE_URL=https://api.example.com/v1
POLICYRAG_LLM_API_KEY=sk-你的密钥
POLICYRAG_LLM_MODEL=模型名称
```

常用项一览（完整注释见 `.env.example`）：

| 分组 | 变量 | 默认 | 说明 |
|---|---|---|---|
| 生成 | `POLICYRAG_LLM_BASE_URL` / `_MODEL` / `_API_KEY` | 本地 8081 / Qwen3.8-27B / 空 | 必填 |
| 生成 | `POLICYRAG_LLM_THINKING` | `disabled` | 推理型模型关闭思考；不认则 `none` |
| 生成 | `RAG_LLM_RETRIES` / `RAG_LLM_FIRST_TOKEN_TIMEOUT` | 2 / 60 | 重试次数与首 token 等待秒数 |
| 并发 | `RAG_GPU_CONCURRENCY` | 2 | 编码/重排共享的 GPU 并发上限 |
| 向量 | `POLICYRAG_VECTOR_BACKEND` | `chroma` | 或 `npz` |
| 向量 | `POLICYRAG_EMBEDDING_DIR` | `models/bge-base-zh-v1.5` | 可绝对路径 |
| 设备 | `POLICYRAG_DEVICE` | `auto` | `cuda` / `cpu` |
| 重排 | `POLICYRAG_RERANK` / `_MODEL_DIR` / `_TOP_N` / `_BATCH` | 1 / `models/bge-reranker-v2-m3` / 20 / 16 | 关掉重排设 `0` |
| OCR | `POLICYRAG_OCR_ENDPOINT` / `_DPI` / `_MIN_CHARS` / `_TIMEOUT` / `_MAX_PAGES` | 空 / 200 / 20 / 180 / 100 | 空 = 不做 OCR |
| docling | `POLICYRAG_PARSER_PDF` / `_DOCLING_PYTHON` / `_ARTIFACTS` / `_DEVICE` / `_TIMEOUT` | `pymupdf` / 同盘 RAG / 同盘 RAG / `cuda` / 600 | 只有显式切 docling 才用 |
| DOCX 页图 | `POLICYRAG_DOCX_PREVIEW` / `_TIMEOUT` | 1 / 120 | 无 Word 会自动跳过 |
| 缓存 | `RAG_CACHE_SIZE` | 256 | 检索结果 LRU 条数 |
| 服务 | `POLICYRAG_HOST` / `POLICYRAG_PORT` | 127.0.0.1 / 7880 | 端口被占用时改这里 |

> 说明：`POLICYRAG_*` 是主前缀；`RAG_LLM_*`、`RAG_GPU_CONCURRENCY`、`RAG_CACHE_SIZE` 是后来加的通用项（历史原因没有统一前缀），按 `.env.example` 原样写即可。
> `.env` 用 UTF-8 保存，不要提交到 Git。

### 10.5 数据：全新入库，还是把旧数据搬过来

**A. 全新环境（推荐先跑通）**：直接灌样例，生成新的 release。

```bash
.venv\Scripts\python.exe scripts/ingest.py knowledge/samples --publish
```

**B. 搬旧数据**（想保留已发布的 release / 文档登记）：

复制这些到新机器的相同相对位置：

| 路径 | 内容 |
|---|---|
| `data/releases/<release_id>/` | 发布的 units/passages/vectors/manifest |
| `data/state/catalog.sqlite` | 文档登记、任务、发布记录 |
| `data/state/active_release.json` | 当前生效指针 |
| `data/docs/<doc_id>/` | 每份文档的原文、units、缓存向量 |
| `data/vector_store/` | chroma 持久化（用 chroma 时） |

**关键坑**：`data/state/active_release.json` 里的 `dir` 是**绝对路径**（例如 `D:\Work\PolicyRAG\data\releases\rel-...`）。
换盘符/换用户名后必须二选一：

1. 手工把 `dir` 改成新机器上的绝对路径；或
2. 调用 `POST /api/releases/{id}/activate` 重新激活，或重新 `python scripts/ingest.py ... --publish` 发布一次。

### 10.6 前端（只有需要改前端 / 没有 dist 时）

`web/dist` 不进 Git。新机器上如果要页面：

```bash
cd web
npm install          # 或 pnpm install
npm run build        # 产物 web/dist，后端会自动托管
```

Node 20/22 均可（本机验证的是 Node 22）。

### 10.7 启动与验收

```bash
.venv\Scripts\python.exe start.py install   # 首次：检查环境、装依赖、构建前端
.venv\Scripts\python.exe start.py           # 启动并打开网页
```

没有页面也能验收：

```bash
curl http://127.0.0.1:7880/api/info            # version、release_id、doc_count、model_ready
curl http://127.0.0.1:7880/api/metrics         # 请求数、错误率、P50/P95
.venv\Scripts\python.exe scripts/smoke_test.py
```

提问验收（应返回流式 NDJSON，最后带 sources）：

```bash
curl -N -X POST http://127.0.0.1:7880/api/ask -H "Content-Type: application/json" -d "{\"question\": \"实验室危险化学品怎么领用？\", \"history\": []}"
```

离线评测：

```bash
.venv\Scripts\python.exe scripts/evaluate.py --dry-run
.venv\Scripts\python.exe scripts/evaluate.py --limit 5
```

### 10.8 新环境常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动报「找不到向量模型」 | `models/` 没拷或路径不对 | 下载/拷贝，或配 `POLICYRAG_EMBEDDING_DIR` |
| 回答出现「生成模型不可用」 | `.env` 三项填错 / 服务没起 | 检查 base_url、key、model；`/api/info` 看 `model_ready` |
| 页面白屏 | 没有 `web/dist` | `cd web && npm install && npm run build` |
| 扫描件入库后没有内容 | OCR 端点未配置 | 配 `POLICYRAG_OCR_ENDPOINT` 或换带文本层的 PDF |
| 启用 docling 后入库报错 | 外部解释器不存在 | 配 `POLICYRAG_DOCLING_PYTHON`，或改回 `pymupdf` |
| DOCX 没有原文页 | 本机没装 Word | 装 Word 后运行 `scripts/docx_preview.py`，或只看文本证据 |
| 换机器后问答提示 release 不存在 | `active_release.json` 里是旧绝对路径 | 改 `dir` 或重新发布/激活 |
| 显存不足 | 两个模型 + 并发占满 | `RAG_GPU_CONCURRENCY=1`、`POLICYRAG_DEVICE=cpu` 或关重排 |
| 端口被占用 | 7880 被别的程序占了 | 改 `POLICYRAG_PORT`，或先 `start.py stop` |
| 中文路径报错 | 个别依赖对中文路径敏感 | 把项目放到纯英文路径（如 `D:/Work/PolicyRAG`） |

### 10.9 最快的整机迁移方式

如果两台机器目录结构一致，直接整包拷贝（排除虚拟环境与依赖缓存）最省事：

```bash
robocopy D:\Work\PolicyRAG E:\Work\PolicyRAG /E /XD .venv node_modules __pycache__ .pytest_cache log
```

拷完在新机器上重建 `.venv` 并 `pip install -r requirements.txt`；`models/`、`data/`、`.env`、`web/dist`
都在包里，直接 `start.py` 即可。如果换了盘符，记得按 10.5 修正 `active_release.json` 的 `dir`。

## 更新记录

### 0.2.1（2026-10-22，T1–T15 优化）

- 检索：bge-reranker 重排、证据组装升级（数值优先/同条款合并/表格行对齐）、LRU 结果缓存、编码/重排 GPU 信号量。
- 入库：DOCX 页图（Word COM → source.pdf）、版本链自动推断与手工覆盖、OCR/docling 后端。
- 生成：拒答与引用后置校验、客户端重试与首 token 超时、分段耗时日志与 `/api/metrics`。
- 前端：重新生成/导出 Markdown/会话重命名、资料库状态与部门筛选、文档详情与停用恢复。
- 验收：177 tests、40 题 40/40、4 并发无 OOM。详见 [docs/优化计划.md](docs/优化计划.md)。
- 文档：README 新增「模型与服务清单」（BGE/重排/OCR/docling/Word/LLM 的配置与接入）与「新工作区迁移清单」。

### 0.2.0

- 架构：配置对象化（`app/settings.py`）、领域包（`app/domain/versions.py`）、发布存储（`app/store/releases.py`）、
  部件契约（`app/core/contracts.py`）、组合根（`app/wiring.py`）；检索/入库/问答各自的流水线拆开，
  详见 [docs/架构.md](docs/架构.md) 与 [docs/复用指南.md](docs/复用指南.md)。
- 检索：新增查询拆解（`app/retrieval/planner.py`），「A 和 B 分别怎么规定」会拆成多个子句分别召回，
  并为每个子问题保留至少一个证据位；40 题评测的跨文档题 5/5。
- 入库：`/api/ingest` 的 `metadata` 支持与 `files` 顺序一致的对象列表，一次上传多份文件可各自登记标题/文号/生效日期。

## 附：一句话排障顺序

```
网页打不开 → 服务起了吗（终端有没有 Uvicorn running）
服务起了但白屏 → 前端 build 了吗（web/dist 存在吗）
能问但报模型不可用 → .env 的 LLM 三项对吗，模型服务通吗
回答没引用 → 先看 /api/info 的 doc_count 是不是 0（知识库没灌）
引用点不开 → 文档是 PDF 吗
```

有问题先看终端日志；仍然解决不了，把「你执行的命令 + 完整报错 + /api/info 输出」发给维护者。
