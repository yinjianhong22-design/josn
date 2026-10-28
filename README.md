# 简表图片识别与数据匹配后端

后端服务，做两件事：把一张简表图片识别成结构化 JSON（`/sheet`），并把识别结果与磁盘上已有的模板库匹配、给出各字段回填值（`/match-sheet`）。技术栈：Python + FastAPI（uvicorn）、OpenCV + NumPy（版面几何）、RapidOCR / ONNX Runtime（文字识别）；仓库不含前端源码，`backend/server.py` 在 `frontend/frontend/dist` 存在时额外做 Vue 前端静态托管，不存在时不影响识别与匹配接口。

开发环境实测为 Windows + Python 3.14.7 + NVIDIA GPU（CUDA 12 运行库经 pip `nvidia-*` 包提供）；生产环境为 Linux 服务器，与开发环境共用同一份代码和 `requirements.txt` 锁文件，仅按需替换个别包（见第 2、7 节）。代码库未定义语义化版本号，以 Git 提交为准（撰写时 HEAD：`457cb18`，2026-10-25）。

核心架构决策：识别与版式分离——OpenCV 只负责线、格、块等几何检测，RapidOCR 只负责识字；同一条识别管线同时服务识别与匹配两类接口，输出内部简表 `{title, context, fields}`；匹配只消费磁盘上已有的 `template.json` / `form.json` 索引，不落库、不回写模板文件，也不提供模板制作/编辑能力；OCR 既可内嵌在识别服务进程内，也可经 `OCR_SERVICE_URL` 拆分为独立 HTTP 服务（默认后者关闭）。

## 1. 功能总览

| 功能 | 接口 | 输入 → 输出 |
|---|---|---|
| 简表图片识别 | `POST /sheet` | multipart 字段 `file`（.png/.jpg/.jpeg/.bmp/.webp）→ 与 `简化.json` 同构的 JSON（11 个固定元数据键 + `分部工程` 嵌套 + `fields[]`，`measured` 缺省 `[]`） |
| 自动路由匹配 | `POST /match-sheet` | 同上 → `{templateUuid, title, context, fields, values, unmatched, ambiguous, sheetImage}` |
| 指定模板匹配 | `POST /templates/{tid}/match-sheet` | 路径参数 `tid` + `file` → 同上（无 `templateUuid`） |
| 模板列表 | `GET /templates` | 无参数 → `[{templateUuid, name, sourceFormat, pageCount}]` |
| 模板详情 | `GET /templates/{tid}` | → `{template: ...}`，页背景相对路径改写为 `/files/...` URL |
| 逐格 OCR（独立服务） | `POST /ocr/cells` | JSON `{image_b64, cells:[{row,col,x,y,w,h}], inset=4}` → `{results:[{row,col,text}]}`，仅含识别出文字的格子 |
| 简表预览图 | `GET /files/_uploads/sheet_preview_*.png` | 匹配成功后按内容哈希生成，不落库删除 |
| 前端静态托管 | `GET /{full_path}` | 仅当 `frontend/frontend/dist` 存在时生效，否则 404 |

**本系统不做什么**（调用方不要依赖以下能力）：

- 不做身份认证与授权，不连接任何外部业务系统，识别结果不落数据库；
- 不提供模板制作、上传转换、编辑、删除接口，只消费磁盘上已有的模板；
- 不修改模板文件本身，匹配结果只是字段值映射；
- 不接受 xlsx / PDF 上传，识别入口只有图片；
- 不限制上传大小、不校验登录态；CORS 为 `*`。

## 2. 环境与安装

已验证环境：

| 项 | 开发（实测） | 生产 |
|---|---|---|
| 操作系统 | Windows | Linux（frp 细节见 `deploy/README.md`） |
| Python | 3.14.7（`requirements.txt` 声明 3.10+） | 3.10+ |
| GPU | NVIDIA + onnxruntime-gpu 1.24.1 | 以实际配置为准，无 N 卡用 CPU 版 |
| 网络 | 仅本机回环 | 经 frp 暴露公网 |

依赖锁定清单（权威来源是 `requirements.txt`，此处照录）：

| pip 包 | 版本 | 用途 |
|---|---|---|
| fastapi | 0.143.0 | Web 框架 |
| uvicorn | 0.54.0 | ASGI 服务器 |
| pydantic | 2.14.1 | 数据模型（ocr_service 显式使用） |
| python-multipart | 0.0.32 | 文件上传 |
| rapidocr-onnxruntime | 1.2.3 | 本地 OCR 引擎（模型随包自带，离线可用） |
| onnxruntime-gpu | 1.24.1 | OCR 推理后端（GPU） |
| nvidia-cublas-cu12 / -cuda-nvrtc / -cuda-runtime / -cudnn / -cufft / -nvjitlink | 12.9.x | CUDA 12 / cuDNN 运行库（仅 GPU 路径） |
| opencv-python | 5.0.0.93 | 图像加载、线/格几何 |
| numpy | 2.5.3 | 数组底座 |
| pillow | 12.3.0 | RapidOCR 依赖 |

最小克隆与安装（Windows 开发机，Git Bash）：

```bash
git clone <仓库地址> && cd test2
py -3.14 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

安装警告（违反必踩坑）：

- `onnxruntime-gpu` 与 `onnxruntime` 互斥（同名模块）。装 GPU 版后**不要**单独 `pip install onnxruntime`，也**不要**重装 rapidocr-onnxruntime（会把 CPU 版 onnxruntime 拉回来）。
- `opencv-python` 与 `opencv-python-headless` 不要同时装。
- `pip check` 提示 rapidocr 声明的 `onnxruntime` 缺失是 GPU 包替代 CPU 包的预期元数据警告，不是安装失败。
- onnxruntime-gpu ≥1.24 的 Windows wheel 不捆绑 CUDA/cuDNN DLL，靠上表 nvidia-* 包提供；加载逻辑由 `ocr_engine.py` 自动把这些包的 `bin` 目录注入 `PATH`，无需手工配置。GPU 是否生效由 `ocr_engine.py` 启动探测：det/cls/rec 三个模型 session 都落在 `CUDAExecutionProvider` 上才算成功，失败打印原因并回退 CPU，不中断识别。
- rapidocr-onnxruntime 1.2.3 的 `UpdateParameters` 有 bug（cls/rec 不剥参数前缀，方向分类和识别模型永远落 CPU），`ocr_engine.py` 内有 monkey-patch；升级该包前先验证补丁是否仍必要。

## 3. 配置与启动

环境变量（全集，无其他隐藏配置）：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `OCR_SERVICE_URL` | 空 | 独立 OCR 服务地址（如 `http://127.0.0.1:8100`）。为空 = 进程内 OCR；设置后服务不可达 = 识别请求 HTTP 500，**不静默回退** |
| `PATH`（代码写入） | — | Windows GPU 路径下自动前置 nvidia-* 包的 `bin` 目录 |

命令行参数：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `server.py --root` | backend/ 的上级目录 | 决定 `_uploads/`、模板扫描根、`/files` 挂载、前端 `dist` 位置 |
| `server.py --host` / `--port` | `127.0.0.1` / 8000 | 仅绑本机；无鉴权，不要直接绑 `0.0.0.0` 暴露公网 |
| `ocr_service.py --host` / `--port` | `127.0.0.1` / 8100 | 模型启动时加载常驻内存，识别请求全局锁串行化 |

配置读取优先级：本系统**没有配置文件、没有 dotenv**，全部配置即上表。优先级为命令行参数 > 默认值；`OCR_SERVICE_URL` 是识别服务切换 OCR 后端的唯一开关（识别管线 CLI 场景下的 `--ocr-url` 优先于环境变量，但 `server.py` 未暴露该参数，对部署无影响）。

两种运行拓扑，二选一：

| 拓扑 | 启动方式 | 适用 |
|---|---|---|
| 单机（默认） | 只起 `server.py`，OCR 引擎在进程内按需加载 | 开发调试、低并发 |
| OCR 分离 | 先起 `ocr_service.py`，再设 `OCR_SERVICE_URL` 后起 `server.py` | GPU 常驻显存、避免每个请求重复加载模型 |

开发启动（Windows，Git Bash / PowerShell 同理）：

```bash
.venv/Scripts/python.exe backend/server.py            # 识别与匹配，127.0.0.1:8000
.venv/Scripts/python.exe backend/ocr_service.py       # 可选：独立 OCR，127.0.0.1:8100
# PowerShell 需先设置: $env:OCR_SERVICE_URL="http://127.0.0.1:8100"
```

启动后的预期输出（对不上说明起错了）：

- `server.py` 打印三行：`表单识别与匹配服务: http://{host}:{port}`、`项目根: {绝对路径}`、前端开发提示；
- `ocr_service.py` 先打印引擎探测结果 `RapidOCR 引擎: CUDA (GPU)`（或 CPU 回退原因），再打印 `RapidOCR 服务监听 http://{host}:{port} (POST /ocr/cells)`；随后用第 4 节的命令验证。

生产启动（Linux，bash）：

```bash
.venv/bin/python backend/server.py --host 127.0.0.1 --port 8000 --root /opt/test2
```

启动占用提示：8000 / 8100 被其他进程占用时 uvicorn 直接报 `Address already in use`，服务不会起来；先 `ss -lnt | grep -E '8000|8100'` 确认空闲。

## 4. 调用与验证

**健康检查语义**：代码中没有专用 `/health` 端点。用 `GET /templates` 当存活检查：返回 200 JSON 数组只证明进程活着且模板扫描跑完，**不代表** OCR 可用、也不代表模板库非空。OCR 是否可用要看 `ocr_service.py` 启动日志中的 `RapidOCR 引擎: CUDA (GPU)`（或 CPU 回退原因）。本服务无鉴权，正常响应里**不会出现 401**；若收到 401，是前置反向代理或 frp 网关发的，与服务无关。

识别与匹配（Git Bash）：

```bash
# 识别：仓库根目录自带示例图；返回结构与 简化.json（完整参照样例）一致
curl -X POST -F "file=@Snipaste_2026-10-14_16-46-15.png" http://127.0.0.1:8000/sheet

# 模板列表（存活检查）
curl http://127.0.0.1:8000/templates

# 自动匹配。注意：当前仓库未内置任何模板库，此命令预期返回 404，
# body 为 {"detail":"没有匹配到模板 (简表表名: ..., 请确认模板库中已有对应模板)"}
curl -X POST -F "file=@Snipaste_2026-10-14_16-46-15.png" http://127.0.0.1:8000/match-sheet

# 指定模板匹配与详情；tid 不存在时返回 404 {"detail":"模板不存在"}
curl -X POST -F "file=@Snipaste_2026-10-14_16-46-15.png" http://127.0.0.1:8000/templates/<tid>/match-sheet
curl http://127.0.0.1:8000/templates/<tid>
```

PowerShell 中 `curl` 是 `Invoke-WebRequest` 别名，必须用 `curl.exe -X POST -F ...` 或 `Invoke-RestMethod`。

独立 OCR 服务验证（bash，先生成请求体）：

```bash
.venv/Scripts/python.exe -c "import base64,json; json.dump({'image_b64': base64.b64encode(open('Snipaste_2026-10-14_16-46-15.png','rb').read()).decode(), 'cells': [{'row':0,'col':0,'x':10,'y':10,'w':200,'h':40}]}, open('body.json','w'))"
curl -X POST http://127.0.0.1:8100/ocr/cells -H "Content-Type: application/json" -d @body.json
```

注意：上面 `cells` 里的坐标是人为构造的单格，只验证连通性和识别通路，不代表真实版面切分结果。

**失败语义**（禁止把以下响应当成功处理）：

| HTTP 状态 | 触发条件（服务侧） |
|---|---|
| 400 | 文件名含路径分隔符/`..`/非法字符/Windows 保留名；扩展名不在白名单；图中未检测到表格；未解析出字段（OCR 无有效结果） |
| 403 | `/files` 路径命中敏感目录（`.git` `.venv` `deploy` `__pycache__` 等）或点文件 |
| 404 | 模板不存在；自动路由无候选或最高分 ≤0；前端未构建 |
| 500 | 未捕获异常，含 `OCR_SERVICE_URL` 指向的服务不可达（不静默回退） |
| 400（ocr_service） | cells 超 10000、base64 非法、解码不成图像 |
| 413（ocr_service） | `image_b64` 超 64 MiB 或像素超 4000 万 |
| 503（ocr_service） | 未安装 rapidocr-onnxruntime |

另：识别边界情况（占位文本、缺列、重名、对齐失败）不会中断整页识别，统一记入 `index.report` 校验报告。

区分"收到响应"和"匹配成功"：`/match-sheet` 返回 200 且 `values={}` 不等于失败（可能是所有值与模板现有文本一致），也不等于成功——必须同时看 `unmatched`（人可读的原因列表，如"某字段 规定值(模板无对应列)"）和 `ambiguous`（名称歧义字段列表，top-2 候选分差 ≤0.05 时记入）。样例输出可对照根目录 `result.json`（一次真实识别结果，含 `null` 元数据）。

## 5. 项目结构

```
test2/                         # 仓库根 = server.py --root 默认值
├── backend/
│   ├── server.py              # 识别与匹配服务：路由、模板扫描、/files 与前端托管
│   ├── ocr_service.py         # 可选独立 OCR 服务：模型常驻内存、全局锁串行化
│   └── template_pipeline/     # 识别与匹配管线（目录即模块索引）
│       ├── constants.py       # 共享常量（OCR 阈值、标签禁词前缀）
│       ├── page_data.py       # 标准页数据契约 build_table_data
│       ├── input/loader.py    # 读图（cv2.imdecode，支持中文路径）
│       ├── layout/lines_pixel.py    # 二值化、横竖线掩码、断线弥合
│       ├── layout/cells.py          # 线交点、格子重建、边判定
│       ├── layout/content.py        # 内容块分析、格内行切分
│       ├── layout/diagnostics.py    # 诊断可视化与落盘
│       ├── text/ocr_engine.py       # RapidOCR 引擎构造：GPU 探测/回退、CUDA DLL 注入、rapidocr 1.2.3 monkey-patch
│       ├── text/ocr_cells.py        # 逐格 OCR（本地或经 OCR_SERVICE_URL 转发）
│       ├── text/ocr_free.py         # 格外游离文字 OCR 与角色标注
│       ├── text/line_roles.py       # 纯几何的行角色分类
│       ├── align/xlsx_anchor.py     # 图片通路的几何伪地址（uuid5 稳定）
│       ├── semantic/roles.py        # 格子角色标注（title/header/label/value/data）
│       ├── semantic/fields.py       # 标签-值字段配对
│       ├── semantic/index.py        # 可填充索引构建 + 校验报告
│       ├── semantic/sheet_input.py  # photo_to_sheet：data → {title, context, fields}
│       ├── semantic/simplified_sheet.py  # sheet_to_simplified：→ 对外简化 JSON
│       ├── semantic/match.py        # match_data_fields：简表 × 模板索引 → 回填值
│       ├── semantic/table_topology.py  # 表头/表体/行标签列发现
│       ├── semantic/column_roles.py    # 列角色（规定值/实测值/合格率）同义词归一
│       └── pipeline/page.py         # extract_page：单页识别总调度
├── deploy/                    # frp 内网穿透配置（frps.toml / frpc.toml），细节见 deploy/README.md
├── _uploads/                  # 运行时生成（识别预览图），已被 .gitignore 忽略
├── requirements.txt           # 依赖锁定清单（唯一权威，禁止手改版本号入另一份文档）
├── 技术方案.md                 # 架构设计、模块逻辑与已验证能力边界
├── 简化.json                   # /sheet 输出结构的完整参照样例
├── result.json                # 一次真实识别的输出样例（含 null 元数据）
├── Snipaste_2026-10-14_16-46-15.png  # 示例输入图
└── README.md                  # 本文档
```

调用链一句话：图片 → `load_image` → 二值化/线掩码/断线弥合/悬垂线过滤 → 交点与格子重建 → 逐格 OCR + 格外游离文字 OCR → `assign_roles` → `extract_fields` → `build_index` → `photo_to_sheet` 得 `{title, context, fields}`；此后 `/sheet` 走 `sheet_to_simplified` 出对外 JSON，`/match-sheet` 走 `match_data_fields` 出 `values/unmatched/ambiguous`。

## 6. 扩展开发指南

新增一个 HTTP 接口（以复用现有识别管线为例）：

1. 在 `backend/server.py` 的 `create_app()` 内加路由；上传解析复用 `_parse_sheet_file()`，它直接给出 `(sheet, index)`，无需自己串管线。
2. 若现有语义不够（新字段类型、新匹配规则），改 `backend/template_pipeline/semantic/` 下对应叶子模块，保持"通用启发式、不针对具体表特判"的约束（见 `技术方案.md` 第五章）。
3. 同步接口清单到两处：`backend/server.py` docstring 与本文档第 1 节表格。
4. 若引入新 pip 依赖：先更新 `requirements.txt`（唯一权威清单），再同步照录它的本文档第 2 节依赖表与安装警告；改环境变量/启动参数时同步本文档第 3 节。

新增一个可匹配模板（运维操作，无需改代码）：把含 `template.json`（必有 `templateUuid`）与同目录 `form.json` 的模板目录放到 `--root` 下任意位置；扫描跳过 `.git` `.venv` `node_modules` `_tmp_form` `_uploads` `_sandbox` `dist` `__pycache__` 及一切点目录；同一 `templateUuid` 多份产物时按 `form.json` 索引规模保留最大者；扫描结果有 2 秒 TTL，新增模板后最多 2 秒生效。

## 7. 生产部署

生产与开发**同一份代码、同一份 `requirements.txt` 锁文件**，差异只有三处：

| 差异点 | 开发 | 生产（Linux 服务器） |
|---|---|---|
| opencv | `opencv-python` | `opencv-python-headless`（无 GUI 依赖） |
| 推理后端 | `onnxruntime-gpu` + nvidia-* 运行库 | 有 N 卡同左；无 N 卡换 `onnxruntime==1.24.1` 并删除全部 nvidia-* 行 |
| 暴露方式 | `127.0.0.1` | 经 frp 内网穿透（`deploy/`），防火墙放通 frp 接入端口与映射端口 |

生产安装（bash，先改 `requirements.txt` 再装，避免装完再换留下冲突包）：

```bash
git clone <仓库地址> && cd test2
# 编辑 requirements.txt：opencv-python -> opencv-python-headless；
# 无 N 卡时 onnxruntime-gpu -> onnxruntime，并删除 6 行 nvidia-*
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python backend/server.py --root /opt/test2 --host 127.0.0.1 --port 8000
```

常驻进程可用 systemd 托管（`ExecStart=/opt/test2/.venv/bin/python backend/server.py --root /opt/test2`，`Restart=always`），与 `deploy/README.md` 中 frps 单元的写法一致。

公网暴露与凭据：

- frp 结构：公网服务器跑 `frps`（接入端口 7000），本机跑 `frpc` 把 `127.0.0.1:8000` / `8100` 映射为公网端口；两端靠 toml 里的共同 token 建立信任，`frps.toml` 的 `allowPorts` 限定可映射端口范围。完整步骤见 `deploy/README.md`。
- **警告**：`deploy/frpc.toml` 与 `deploy/frps.toml` 中的 token 当前是明文入库的，对外使用前必须更换；token 属于凭据，不要提交到 Git（如需重建配置，用 `<FRP_TOKEN>` 占位后本地填入真实值）。
- **警告**：两个服务均无鉴权、无上传大小限制、CORS 为 `*`；端口映射到公网后任何人都能调用，仅在需要时启动 frpc，或在服务器防火墙做来源 IP 白名单。

## 8. 测试说明

本仓库当前没有自动化测试，以下三类各自覆盖范围如实说明：

| 类型 | 覆盖 | 不覆盖 |
|---|---|---|
| 离线测试 | 无 pytest/unittest 套件；最低限度验证 = 两个服务能启动、`GET /templates` 返回 200 | 任何识别与匹配逻辑的正确性 |
| 真实模型验收 | 用根目录 `Snipaste_2026-10-14_16-46-15.png` 调 `/sheet`，对照 `简化.json` 的结构与字段；GPU 路径核对启动日志 `RapidOCR 引擎: CUDA (GPU)`；匹配验收需自备模板库后调 `/match-sheet` 并人工核对 `values` / `unmatched` | OCR 准确率上限（手写、低清图）、无线表格返回 0 格、模板索引质量导致的匹配失败——这些已知边界见 `技术方案.md` 第六章 |
| CI | 无（仓库无 CI 配置与流水线） | 一切；发版前靠人工跑上述验收清单 |

## 9. 参考链接

- 仓内文档：`技术方案.md`（架构与能力边界）、`deploy/README.md`（frp 部署细则）
- FastAPI：https://fastapi.tiangolo.com/
- RapidOCR：https://github.com/RapidAI/RapidOCR
- ONNX Runtime：https://onnxruntime.ai/
- frp（内网穿透）：https://github.com/fatedier/frp/releases
