# 本地资料检索

这是一个 Docker 化的本地资料 OCR 与全文检索项目。把 PDF、TXT、MD、DOC、DOCX 放进 `data/`，系统会自动扫描、索引，并在网页中提供全文搜索。

网页入口：

```powershell
http://localhost:8517
```

## 功能

- 自动扫描 `data/` 及其子目录，支持 `.pdf`、`.txt`、`.md`、`.doc`、`.docx`。
- PDF OCR 改为调用 PaddleOCR 官方 API，模型固定为 `PP-OCRv6`，不再本地加载 PaddleOCR，也不再占用本机 GPU/CPU 跑 OCR。
- PDF 处理完成后会写回原路径：保留原 PDF 页面内容，只追加不可见文字层，不再把页面渲染成图片 PDF。
- TXT/MD/DOC/DOCX 会建立本地 SQLite/FTS 索引。
- 检索索引会额外写入简体/繁体变体；原文档不被修改，用户可用简体或繁体搜索。
- 前端可限制搜索范围，例如只搜 `pdf/论文`。
- PDF OCR 后会取前五页和后五页文本，请求 DeepSeek 提取出版信息，并在结果中提供“导出引用”。
- 结果右上角“打开”会通过本机打开助手调用系统默认程序打开 `data/` 中的原始文件；“查看 OCR 版”仍在浏览器中预览 PDF。
- Docker Compose 使用 `restart: unless-stopped`，可配合 Windows 计划任务开机自启。

## 资料目录

资料只扫描 `data/`。可以任意建立分类：

```text
data/
  txt/
  doc/
  pdf/
    诗话/
    古籍/
    论文/
```

前端“范围”下拉会自动读取这些目录。默认搜索全部资料。

## 配置

本机使用 `.env` 保存密钥；不要把 `.env` 提交到版本库。

```env
OCR_ENGINE=api
OCR_DEVICE=api
PADDLEOCR_API_TOKEN=
PADDLEOCR_API_MODEL=PP-OCRv6
PADDLEOCR_API_POLL_SECONDS=5
PADDLEOCR_API_TIMEOUT_SECONDS=7200
PADDLEOCR_API_REQUEST_TIMEOUT_SECONDS=300
PADDLEOCR_USE_DOC_ORIENTATION_CLASSIFY=false
PADDLEOCR_USE_DOC_UNWARPING=false
PADDLEOCR_USE_TEXTLINE_ORIENTATION=false

DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-pro
PUBLICATION_EXTRACT_ENABLED=true
LOCAL_OPEN_ENABLED=true
```

说明：

- `PADDLEOCR_API_TOKEN`：PaddleOCR API access token。
- `PADDLEOCR_API_TIMEOUT_SECONDS`：单个 OCR 任务最长等待时间。
- `PADDLEOCR_API_POLL_SECONDS`：轮询 OCR 任务进度的间隔。
- `DEEPSEEK_API_KEY`：未配置时仍可 OCR 和搜索，只是不生成出版引用。
- `LOCAL_OPEN_ENABLED`：控制后端本地运行时是否允许打开文件；Docker 运行时由 `scripts/open-helper.py` 处理。

## 启动

推荐：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

这个脚本会同时启动 Docker 服务和本机文件打开助手。若只执行 `docker compose up -d --build`，搜索仍可用，但网页里的“打开本地文件”按钮无法启动宿主机应用。

如果你仍使用旧的 GPU overlay 命令也可以，当前 overlay 不会请求 GPU：

```powershell
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

查看日志：

```powershell
docker compose logs -f backend worker
```

## 开机自启

安装计划任务：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
```

取消自启：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\scripts\uninstall-autostart.ps1
```

计划任务会在当前 Windows 用户登录后执行启动脚本。Docker Desktop 本身也需要设置为随系统启动。

## PDF 写回方式

当前版本上传原 PDF 到 PaddleOCR API，拿到 OCR 文本与坐标后，在原 PDF 页面上追加不可见文字层，再保存回原路径。

这意味着新处理的 PDF 不会因为“把每页转成固定 DPI 图片再打包”而膨胀成超大文件。若某个 PDF 已经被旧版本覆盖成图片型大 PDF，程序无法凭空恢复原始体积；需要用原始 PDF 替换后重新处理。

## 重新处理

正常增删改会自动触发处理，也可以手动扫描：

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/api/scan
```

彻底重建索引：

```powershell
docker compose down
Remove-Item -Recurse -Force .\.docsearch
docker compose up -d --build
```

## 端口

- `8517`：网页
- `8000`：后端 API
- `8765`：本机文件打开助手，仅监听 `127.0.0.1`
