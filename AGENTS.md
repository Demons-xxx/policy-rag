# 项目约定

- 使用说明：`README.md`（面向零经验协作者，先读它）。
- 接口与数据格式的唯一权威：`docs/架构与接口.md`。改接口前先改这份文档。
- 目录职责：`app/api` 接口、`app/ingest` 入库、`app/retrieval` 检索、`app/llm` 生成、`app/store` 元数据、`web` 前端、`knowledge` 样例制度。
- 运行环境：`D:\Work\PolicyRAG\.venv\Scripts\python.exe`；服务端口 7880；pip 必须加镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。
- 启动：`python -m uvicorn app.main:app --port 7880`；测试：`python -m pytest tests -q`；入库：`python scripts/ingest.py <路径> --publish`。
- 版本与生效数据分别以 `app/project.json` 和 `data/state/active_release.json` 为准；`/api/info` 反映实际运行状态。
- 模型权重（`models/`）、运行数据（`data/`）、前端构建产物（`web/dist`）、`.env` 不进 Git。
- 不修改 `knowledge/` 下的既有样例做实验；测试样本放 `tests/fixtures/`。
- 文件写入统一 UTF-8；原子写用 `.tmp` + `os.replace`；错误信息用中文并带具体原因。
