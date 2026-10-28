# 通过 frp 把本机 OCR 服务暴露到公网

结构：

- 公网服务器 `107.182.190.191` 运行 **frps**（`frps.toml`）
- 本机运行 **frpc**（`frpc.toml`），把 `127.0.0.1:8100`（ocr_service.py）转发为公网 `107.182.190.191:18100`
- 鉴权用 token（两个 toml 里必须一致），且 frps 端 `allowPorts` 只允许 18100-18109

## 一、公网服务器部署 frps（Linux 示例）

```bash
# 1. 下载并解压（以 amd64 为例，按实际架构选择）
wget https://github.com/fatedier/frp/releases/download/v0.71.1/frp_0.71.1_linux_amd64.tar.gz
tar xzf frp_0.71.1_linux_amd64.tar.gz && cd frp_0.71.1_linux_amd64

# 2. 复制本目录的 frps.toml 到服务器，然后启动
./frps -c ./frps.toml
```

生产环境建议用 systemd 常驻（`/etc/systemd/system/frps.service`）：

```ini
[Unit]
Description=frp server
After=network.target

[Service]
ExecStart=/opt/frp/frps -c /opt/frp/frps.toml
Restart=always

[Install]
WantedBy=multi-user.target
```

防火墙/安全组放通：**TCP 7000**（frpc 接入）和 **TCP 18100**（OCR 访问端口）。

## 二、本机运行 frpc（Windows）

前置：本机已加 Defender 排除项（frpc 常被误报，见 frp issue 3637），
frpc.exe 已解压到 `deploy/bin/`。

```bash
# 1. 启动 OCR 服务（另开一个终端）
.venv/Scripts/python.exe backend/ocr_service.py

# 2. 启动 frpc（本项目根目录）
./deploy/bin/frpc.exe -c deploy/frpc.toml
```

## 三、验证

```bash
# 经公网调用 OCR（与 ocr_service 的接口一致）
curl -X POST http://107.182.190.191:18100/ocr/cells -H "Content-Type: application/json" -d @body.json
```

`body.json` 格式见 `ocr_service.py` 的 `/ocr/cells` 接口定义。

注意：OCR 服务本身无鉴权，18100 端口对公网开放后任何人都能调用，
建议仅在需要时启动 frpc，或在服务器防火墙做来源 IP 白名单。

## 改动指引

- 换公网端口：改 `frpc.toml` 的 `remotePort`，并确认在 `frps.toml` 的 `allowPorts` 范围内
- 再加一个内网服务（如 8000 端口的模板服务）：在 `frpc.toml` 加一个 `[[proxies]]`，`remotePort` 选 18101 等
- 换服务器：改两个 toml 里的 `serverAddr` / token 即可
