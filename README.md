# 本地资料检索

本项目是一个 Docker 化的本地资料 OCR、索引与全文检索系统。把 PDF、CAJ、TXT、MD、DOC、DOCX 放进 `data/` 目录后，系统会自动扫描、处理、建立 SQLite/FTS 全文索引，并在网页端提供搜索、按文件聚合结果、PDF 命中页预览、高亮、引用导出和本地文件打开。

网页入口：

```text
http://localhost:8517
```

后端 API：

```text
http://localhost:8000
```

## 项目结构

```text
.
├─ backend/                 FastAPI 后端、索引器、OCR、文件转换逻辑
├─ frontend/                React + Vite 前端
├─ scripts/                 Windows/Linux 启停脚本、本地文件打开助手
├─ data/                    资料目录，用户文件放这里
├─ .docsearch/              运行状态目录，保存数据库、临时文件、预览文件
├─ .env.example             环境变量示例
└─ docker-compose.yml       Docker Compose 服务定义
```

运行时主要有三个容器：

- `frontend`：Nginx 托管前端页面，默认端口 `8517`。
- `backend`：FastAPI API 服务，默认端口 `8000`。
- `worker`：后台处理队列，负责 OCR、转换、索引。

另外，Windows 下推荐启动 `scripts/open-helper.py`，它监听 `127.0.0.1:8765`，用于网页“打开本地文件”按钮调用宿主机默认程序。

## 功能概览

- 自动扫描 `data/` 及子目录。
- 支持 `.pdf`、`.caj`、`.txt`、`.md`、`.doc`、`.docx`。
- PDF 默认调用 PaddleOCR 官方 API，模型默认为 `PP-OCRv6`。
- PDF OCR 后会在原 PDF 页面上追加不可见文字层，并写回原路径。
- `data/pdf/论文/` 下的 PDF 默认只读取内嵌文字，不重新 OCR。
- CAJ 会先转成可搜索 PDF，再抽取文字；若转换后文字不足，会继续 OCR。
- DOC/DOCX 会抽取文字，并转换 PDF 用于预览。
- TXT/MD 直接抽取文本并入库。
- 搜索索引会写入简体/繁体变体，简繁关键词都能检索。
- 搜索结果按文件聚合，展开后显示文件内命中条目。
- 支持普通搜索、同一行多关键词搜索、同一文件多关键词搜索。
- PDF 命中结果右侧显示对应页图片，并高亮命中位置。
- 可按目录限制搜索范围，例如只搜 `pdf/论文`。
- PDF 处理后可调用 DeepSeek 从前后页提取出版信息，并生成引用。
- 前端显示 OCR API 当日额度使用情况，例如 `14304/20000`。
- 重新扫描时可选择“重试失败文件”，把失败或空结果文件重新加入队列。

## 安装前准备

### Windows 推荐环境

- Windows 10/11
- Docker Desktop
- PowerShell 5+ 或 PowerShell 7+
- Python 3，用于本机文件打开助手

Docker Desktop 需要已经启动。首次构建镜像会下载依赖，时间可能较长。

### 账号与密钥

OCR 使用 PaddleOCR API，需要在 `.env` 中配置：

```env
PADDLEOCR_API_TOKEN=你的 PaddleOCR API Token
```

出版信息提取使用 DeepSeek，可选：

```env
DEEPSEEK_API_KEY=你的 DeepSeek API Key
```

不配置 DeepSeek 时，OCR 和搜索仍可正常工作，只是不自动生成出版引用。

## 快速开始

1. 复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

2. 编辑 `.env`，至少填写：

```env
PADDLEOCR_API_TOKEN=
```

也可以先不填 token 直接启动。首次打开网页时，如果 `PADDLEOCR_API_TOKEN` 或 `DEEPSEEK_API_KEY` 未配置，左侧 `OCR 设备` 卡片会显示输入框；保存后配置会写入 `.docsearch/runtime-config.json`，后端和 worker 会在后续任务中直接使用。

3. 把资料放进 `data/`，例如：

```text
data/
  pdf/
    专著/
    论文/
  txt/
  doc/
```

4. 启动系统：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

该脚本会：

- 创建 `data/` 和 `.docsearch/`。
- 启动本机文件打开助手 `127.0.0.1:8765`。
- 等待 Docker 可用。
- 执行 `docker compose up -d --build`。

5. 打开网页：

```text
http://localhost:8517
```

如果只想启动 Docker 容器，不需要“打开本地文件”按钮：

```powershell
docker compose up -d --build
```

## 停止与重启

停止容器和本机文件打开助手：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
```

只停止 Docker：

```powershell
docker compose down
```

重启并重新构建：

```powershell
docker compose up -d --build
```

查看服务状态：

```powershell
docker compose ps
```

查看日志：

```powershell
docker compose logs -f backend worker
```

## 资料目录与扫描规则

系统只扫描 Docker 中的 `/library`，它绑定到本机的 `./data`。

推荐按类型和主题建立目录：

```text
data/
  pdf/
    专著/
    论文/
    诗集/
  caj/
  txt/
  doc/
```

前端“范围”下拉会自动读取 `data/` 下的目录。选择某个范围后，只搜索该目录下的文件。

扫描器会跳过：

- `.docsearch`
- `.git`
- `.svn`
- `node_modules`
- `__pycache__`
- `.pytest_cache`
- `.venv`
- `venv`
- `EXCLUDE_DIRS`、`EXCLUDE_PATHS` 配置中排除的目录或路径

当前 Docker 配置默认处理 `.pdf`、`.txt`、`.md`、`.doc`、`.docx`、`.caj`。

### 什么时候会重新处理文件

扫描器根据文件路径、大小、修改时间和首尾内容指纹判断变化。以下情况会入队：

- 新增文件。
- 文件大小或修改时间变化。
- 文件内容指纹变化。
- 旧文件从 `data/` 删除。
- 点击“重新扫描”并勾选“重试失败文件”时，失败或空结果文件会重新入队。

## OCR 与文件处理流程

### PDF

普通 PDF 的流程：

1. 上传 PDF 到 PaddleOCR API。
2. 轮询 OCR 任务。
3. 下载识别结果。
4. 根据 OCR 返回的文本和坐标，在原 PDF 每页追加不可见文字层。
5. 将可搜索 PDF 写回原路径。
6. 抽取文本 chunk，写入 SQLite/FTS 索引。
7. 如启用出版信息提取，取前五页和后五页文本调用 DeepSeek。

注意：当前版本不是把每页渲染成图片 PDF，而是在原页面上追加文字层，因此能保留原页面内容并避免 PDF 体积异常膨胀。

### `pdf/论文` 目录

默认配置：

```env
PDF_TEXT_ONLY_PATHS=pdf/论文
```

匹配该目录的 PDF 会直接读取已有文字层，不调用 OCR。适合已经可复制文字的论文 PDF。

可配置多个路径，用逗号分隔：

```env
PDF_TEXT_ONLY_PATHS=pdf/论文,pdf/已OCR
```

### CAJ

CAJ 文件流程：

1. 使用 `CAJ_CONVERTER_COMMAND` 转 PDF。
2. 抽取转换后 PDF 的文字。
3. 如果文字量低于 `OCR_MIN_TEXT_CHARS`，再对转换后的 PDF 调用 OCR。
4. 转换后的 PDF 存在 `.docsearch/preview/`，用于预览和搜索。

默认转换命令：

```env
CAJ_CONVERTER_COMMAND=caj2pdf convert {input} -o {output}
```

后端镜像已经安装默认 `caj2pdf`。如需改用其他转换器，命令必须能接收输入和输出路径。

### TXT / MD

直接抽取纯文本，按行或文本块建立索引。

### DOC / DOCX

抽取 Word 文本，同时用 LibreOffice 转成 PDF 供前端预览。

## 搜索使用说明

### 普通搜索

在搜索框输入一个关键词，例如：

```text
海藏
```

结果按文件聚合显示。每个文件一行，右侧显示命中数量。点击文件行展开该文件内的命中条目，再点击可收起。

点击具体命中条目后：

- 如果是 PDF，右侧显示命中所在页，并高亮位置。
- 如果是文本类文件，右侧显示上下文。
- 可点击右上角“打开”调用本机默认程序打开原始文件。

### 多关键词搜索

搜索框支持用空格、逗号、顿号、分号分隔多个关键词：

```text
郑孝胥 袁世凯
郑孝胥，袁世凯
郑孝胥、袁世凯
```

如果关键词本身包含空格，可使用引号：

```text
"New Culture" 胡适
```

### 匹配模式

搜索框下方有三个模式：

- `普通`：默认模式。按输入内容搜索，适合单关键词或短语。
- `同一行`：多个关键词必须同时出现在同一个 OCR 行或同一个文本 chunk。
- `同一文件`：多个关键词可以出现在同一个文件的不同位置，只要同一个文件包含所有关键词即可。

例子：

```text
郑孝胥 袁世凯
```

- `同一行`：只找同一行里同时出现“郑孝胥”和“袁世凯”的位置。
- `同一文件`：找同时包含“郑孝胥”和“袁世凯”的文件，展开后显示这两个关键词相关的命中条目。

### 搜索范围

右上方“范围”下拉来自 `data/` 目录结构。例如选择：

```text
pdf/论文
```

则只搜索 `data/pdf/论文/` 下面的文件。

## 前端界面说明

左侧栏：

- `已索引`：当前 ready 状态文档数。
- `待处理`：等待处理的任务数。
- `OCR 设备`：显示 `API` 和当日页数额度，例如 `API 14304/20000`。
- `资源策略`：当前自动资源限制概要。
- `重新扫描`：手动扫描 `data/`。
- `重试失败文件`：勾选后，重新扫描会把失败或空结果文件重新加入队列。
- `处理队列`：当前处理进度和最近任务。
- `最近变化`：新增、索引、失败、删除等事件。

中间栏：

- 搜索框。
- 匹配模式。
- 搜索范围。
- 按文件聚合的结果列表。
- 展开文件后的具体命中列表。

右侧栏：

- PDF 命中页预览。
- 搜索词和 OCR 坐标高亮。
- 文本上下文。
- “打开”按钮，调用宿主机默认程序打开原文件。
- “导出引用”按钮，在结果中存在出版信息时出现。

## 配置详解

配置文件为项目根目录 `.env`。可从 `.env.example` 复制。

### OCR 配置

```env
OCR_ENGINE=api
OCR_VERSION=PP-OCRv6
OCR_DEVICE=api
PADDLEOCR_API_URL=https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
PADDLEOCR_API_TOKEN=
PADDLEOCR_API_MODEL=PP-OCRv6
PADDLEOCR_DAILY_PAGE_LIMIT=20000
PADDLEOCR_QUOTA_TIMEZONE=Asia/Shanghai
PADDLEOCR_API_BATCH_PAGES=100
PADDLEOCR_API_TRANSPORT_RETRIES=4
PADDLEOCR_API_POLL_SECONDS=5
PADDLEOCR_API_TIMEOUT_SECONDS=7200
PADDLEOCR_API_REQUEST_TIMEOUT_SECONDS=300
PADDLEOCR_USE_DOC_ORIENTATION_CLASSIFY=false
PADDLEOCR_USE_DOC_UNWARPING=false
PADDLEOCR_USE_TEXTLINE_ORIENTATION=false
```

说明：

- `PADDLEOCR_API_TOKEN`：PaddleOCR API Token。
- `PADDLEOCR_API_MODEL`：OCR 模型，默认 `PP-OCRv6`。
- `PADDLEOCR_DAILY_PAGE_LIMIT`：每日页数额度，默认 `20000`。
- `PADDLEOCR_QUOTA_TIMEZONE`：额度自然日时区，默认北京时间。
- `PADDLEOCR_API_BATCH_PAGES`：大 PDF 分批提交页数。
- `PADDLEOCR_API_TRANSPORT_RETRIES`：API 网络请求重试次数。
- `PADDLEOCR_API_POLL_SECONDS`：轮询 OCR 任务间隔。
- `PADDLEOCR_API_TIMEOUT_SECONDS`：单个 OCR 任务最长等待时间。
- `PADDLEOCR_API_REQUEST_TIMEOUT_SECONDS`：单次 HTTP 请求超时。
- `PADDLEOCR_USE_*`：PaddleOCR API 的方向分类、文档矫正、行方向选项。

### 文件与资源限制

```env
OCR_MIN_TEXT_CHARS=80
OCR_MAX_PAGES=0
MAX_FILE_MB=0
MAX_OUTPUT_PDF_MB=0
RESOURCE_AUTO_TUNE=true
SCAN_INTERVAL_SECONDS=20
EXCLUDE_DIRS=.docsearch,.git,.svn,node_modules,__pycache__,.pytest_cache,.venv,venv
EXCLUDE_PATHS=
PDF_TEXT_ONLY_PATHS=pdf/论文
CAJ_CONVERTER_COMMAND=caj2pdf convert {input} -o {output}
CAJ_CONVERTER_TIMEOUT_SECONDS=600
```

说明：

- `OCR_MIN_TEXT_CHARS`：判断文档是否已有足够文字的阈值。
- `OCR_MAX_PAGES=0`：不限制页数；设置正数可限制 OCR 页数。
- `MAX_FILE_MB=0`：使用自动资源策略；设置正数可强制文件大小上限。
- `MAX_OUTPUT_PDF_MB=0`：使用自动资源策略；设置正数可强制输出 PDF 大小上限。
- `RESOURCE_AUTO_TUNE=true`：根据内存/GPU 情况选择文件大小、DPI、页像素等限制。
- `SCAN_INTERVAL_SECONDS`：后端自动扫描间隔。
- `EXCLUDE_DIRS`：按目录名排除。
- `EXCLUDE_PATHS`：按相对路径排除，逗号分隔。
- `PDF_TEXT_ONLY_PATHS`：只抽取文字、不 OCR 的 PDF 目录。
- `CAJ_CONVERTER_TIMEOUT_SECONDS`：单个 CAJ 转换超时秒数。

### 出版信息提取

```env
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
DEEPSEEK_TIMEOUT_SECONDS=120
DEEPSEEK_MAX_CHARS=60000
PUBLICATION_EXTRACT_ENABLED=true
```

说明：

- 只对 PDF 执行出版信息提取。
- 默认取前五页和后五页文本作为上下文。
- 未配置 `DEEPSEEK_API_KEY` 时，索引不受影响，只是不生成引用。
- 可用脚本写入 DeepSeek Key：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\set-deepseek-key.ps1
```

### 本地打开文件

```env
LOCAL_OPEN_ENABLED=true
```

Docker 容器本身不能直接打开 Windows 桌面程序，所以推荐用 `scripts/start.ps1` 启动。该脚本会启动 `scripts/open-helper.py`，前端会优先调用：

```text
http://127.0.0.1:8765/open
```

如果打开失败：

1. 确认是用 `scripts/start.ps1` 启动。
2. 检查 `.docsearch/open-helper.err.log`。
3. 访问 `http://127.0.0.1:8765/health` 看助手是否在线。

## 常用维护命令

### 手动扫描

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/scan
```

重试失败或空结果文件：

```powershell
Invoke-RestMethod -Method Post "http://localhost:8000/api/scan?retry_failed=true"
```

### 查看状态

```powershell
Invoke-RestMethod http://localhost:8000/api/stats
Invoke-RestMethod http://localhost:8000/api/jobs
Invoke-RestMethod http://localhost:8000/api/events
```

### 搜索 API 示例

普通聚合搜索：

```powershell
Invoke-RestMethod "http://localhost:8000/api/search/groups?q=海藏&limit=10"
```

同一行多关键词：

```powershell
Invoke-RestMethod "http://localhost:8000/api/search/groups?q=郑孝胥%20袁世凯&mode=line&limit=10"
```

同一文件多关键词：

```powershell
Invoke-RestMethod "http://localhost:8000/api/search/groups?q=郑孝胥%20袁世凯&mode=document&limit=10"
```

查看某个文件内的命中：

```powershell
Invoke-RestMethod "http://localhost:8000/api/search/document/<document_id>?q=海藏&limit=50"
```

### 重建索引

如果想保留原始资料，只重建数据库和临时状态：

```powershell
docker compose down
Remove-Item -Recurse -Force .\.docsearch
docker compose up -d --build
```

注意：`.docsearch/` 包含 SQLite 索引、预览 PDF、OCR 临时文件和打开助手日志。删除后系统会重新扫描并处理 `data/`。

## 开机自启

安装计划任务：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
```

取消计划任务：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\uninstall-autostart.ps1
```

计划任务会在当前 Windows 用户登录后运行 `scripts/start.ps1`。Docker Desktop 本身也需要设置为随系统启动。

## 端口与数据卷

端口：

- `8517`：网页前端。
- `8000`：后端 API。
- `8765`：本机文件打开助手，只监听 `127.0.0.1`。

数据卷：

- `./data` -> 容器 `/library`：资料文件。
- `./.docsearch` -> 容器 `/data`：数据库、预览、临时状态。

## 故障排查

### 网页打不开

检查容器：

```powershell
docker compose ps
docker compose logs --tail=100 frontend backend
```

确认端口没有被占用：

```powershell
netstat -ano | findstr ":8517"
netstat -ano | findstr ":8000"
```

### 文件一直待处理

查看 worker 日志：

```powershell
docker compose logs -f worker
```

查看任务：

```powershell
Invoke-RestMethod http://localhost:8000/api/jobs
```

如果任务失败后想重新处理，网页勾选“重试失败文件”再点“重新扫描”，或调用：

```powershell
Invoke-RestMethod -Method Post "http://localhost:8000/api/scan?retry_failed=true"
```

### OCR API 报错

检查：

- `.env` 中 `PADDLEOCR_API_TOKEN` 是否填写。
- API Token 是否过期。
- 当日额度是否用完。
- `docker compose logs -f worker` 中的具体错误。

前端左侧 `OCR 设备` 会显示当日用量和额度。额度按 `PADDLEOCR_QUOTA_TIMEZONE` 的自然日统计。

### PDF 命中页没有高亮

可能原因：

- 旧索引没有 OCR 坐标信息。
- PDF 页面文字和图片坐标本身不一致。
- 查询词和 OCR 结果存在繁简、断行或识别差异。

可尝试删除 `.docsearch/` 后重建索引，或替换原始 PDF 后重新处理。

### “打开”按钮无效

确认：

- 使用 `scripts/start.ps1` 启动。
- `http://127.0.0.1:8765/health` 可访问。
- `.docsearch/open-helper.err.log` 没有错误。
- 文件仍在 `data/` 目录内，没有被移动或删除。

### CAJ 转换失败

检查：

- `CAJ_CONVERTER_COMMAND` 是否正确。
- 文件是否损坏或加密。
- `CAJ_CONVERTER_TIMEOUT_SECONDS` 是否太短。
- `docker compose logs -f worker` 中的转换错误。

## 开发

后端本地检查：

```powershell
python -m unittest discover backend\tests
python -m py_compile backend\app\database.py backend\app\main.py
```

前端构建：

```powershell
cd frontend
npm ci
npm run build
```

Docker 构建：

```powershell
docker compose up -d --build backend worker frontend
```

前端开发服务器：

```powershell
cd frontend
npm run dev
```

开发服务器默认由 Vite 提供；生产使用 Docker 中的 Nginx。

## 注意事项

- `.env` 包含密钥，不要提交。
- `data/` 是用户资料目录，处理 PDF 时可能会写回可搜索文字层；重要原始文件建议另行备份。
- `.docsearch/` 可以删除重建，但删除后索引和预览都需要重新生成。
- 当前 OCR 主要面向 PaddleOCR API；本地 GPU/CPU OCR 不是默认路径。
- 大型 PDF、超大 CAJ 或复杂扫描件可能耗时较长，应优先查看 worker 日志判断进度。
