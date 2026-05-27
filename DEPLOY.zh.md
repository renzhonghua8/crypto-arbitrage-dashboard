# Windows 服务器部署指南（日本/海外 IP）

> 中文 · [English](DEPLOY.md)

部署架构：

```
  互联网用户
       │  HTTPS
       ▼
  Cloudflare Tunnel  (cloudflared.exe 作为 Windows 服务)
       │  HTTP，仅本机回环
       ▼
  uvicorn (NSSM Windows 服务，监听 127.0.0.1:8765)
       │  Basic Auth 中间件 (账号密码从 .env 读)
       ▼
  ccxt → Binance / OKX / Bybit / Gate / Bitget
```

为什么这样：
- **Cloudflare Tunnel**：免费、自动 HTTPS、不需要给你的服务器开 80/443 端口、隐藏服务器真实 IP。
- **NSSM**：把 uvicorn 注册成 Windows 服务，开机启动 + 崩溃自动重启。
- **Basic Auth**：服务被 Cloudflare 暴露在公网，没有认证任何人都能看，加一层用户名密码。

---

## 前提

- Windows Server 2019+ 或 Windows 10/11 Pro
- 已装 Python 3.10+ 和 git
- 已通过 RDP 登录到服务器
- 全部命令在 **以管理员身份运行的 PowerShell** 中执行

确认环境：

```powershell
python --version    # 应 ≥ 3.10
git --version
```

---

## 第 1 步：拉代码

```powershell
# 目录任选，这里用 C:\apps
mkdir C:\apps -Force
cd C:\apps
git clone https://github.com/renzhonghua8/crypto-arbitrage-dashboard.git
cd crypto-arbitrage-dashboard

# 装 Python 依赖
pip install -r requirements.txt
```

---

## 第 2 步：配置 .env

```powershell
# 复制模板
Copy-Item .env.example .env
notepad .env
```

编辑成下面这样（**改密码！**）：

```env
# 日本 IP 直连，不需要代理
HTTPS_PROXY=
HTTP_PROXY=

# Basic Auth — 改成你自己的强密码
AUTH_USER=admin
AUTH_PASS=随便填一串至少 12 位的强密码
```

保存关闭。

---

## 第 3 步：本地试跑一下

```powershell
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8765
```

新开一个 PowerShell：

```powershell
# 不带认证应该 401
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8765/
# 带正确账号密码应该 200
curl.exe -s -o NUL -w "%{http_code}`n" -u admin:你的密码 http://127.0.0.1:8765/
```

看到 `401` 和 `200` 就 OK。回到第一个窗口按 `Ctrl+C` 停掉。

---

## 第 4 步：装 NSSM 把 uvicorn 注册成 Windows 服务

```powershell
# 装 NSSM（如已装跳过）
winget install --id NSSM.NSSM -e --silent

# 找一下 Python 的绝对路径
$pythonPath = (Get-Command python).Source
Write-Host "Python at: $pythonPath"

# 注册服务（一行命令搞定，不用走 GUI）
nssm install ArbDashboard $pythonPath -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8765
nssm set ArbDashboard AppDirectory C:\apps\crypto-arbitrage-dashboard
nssm set ArbDashboard DisplayName "Crypto Arbitrage Dashboard"
nssm set ArbDashboard Description "Real-time crypto arbitrage scanner"
nssm set ArbDashboard Start SERVICE_AUTO_START
nssm set ArbDashboard AppStdout C:\apps\crypto-arbitrage-dashboard\logs\stdout.log
nssm set ArbDashboard AppStderr C:\apps\crypto-arbitrage-dashboard\logs\stderr.log
nssm set ArbDashboard AppRotateFiles 1
nssm set ArbDashboard AppRotateBytes 10485760

# 创建日志目录
mkdir C:\apps\crypto-arbitrage-dashboard\logs -Force

# 启动
nssm start ArbDashboard

# 看状态
nssm status ArbDashboard           # 应输出 SERVICE_RUNNING
Get-Content C:\apps\crypto-arbitrage-dashboard\logs\stderr.log -Wait -Tail 20
```

第一次 fetch_all 大约 15-30 秒，等到日志里看到类似 `snapshot: 6305 tickers | funding=200 ...` 就成功了。`Ctrl+C` 退出日志查看。

验证：

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" -u admin:你的密码 http://127.0.0.1:8765/api/snapshot
# 期望: 200
```

---

## 第 5 步：装 Cloudflare Tunnel（暴露到公网，自动 HTTPS）

### 5.1 下载 cloudflared

```powershell
# 下载 64 位 Windows 版
Invoke-WebRequest `
  -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
  -OutFile "C:\Windows\System32\cloudflared.exe"

cloudflared --version    # 验证
```

### 5.2 创建临时 Tunnel（5 分钟启动法，拿一个 *.trycloudflare.com 域名）

```powershell
# 前台跑一遍，看输出里的 URL
cloudflared tunnel --url http://127.0.0.1:8765
```

输出里会有一行：

```
Your quick Tunnel has been created! Visit it at:
https://xxxx-xxxx-xxxx-xxxx.trycloudflare.com
```

打开这个 URL，浏览器会弹出 Basic Auth 登录框（用 .env 里的账号密码）。

`Ctrl+C` 停掉，下一步注册成服务。

### 5.3 把 cloudflared 也注册成 Windows 服务

> ⚠ 临时 trycloudflare 域名每次重启会变。如果你不需要固定域名，下面的服务化用 `--url` 即可；如果想要固定域名，需要 Cloudflare 账号 + 自有域名，见文末。

```powershell
# 在 NSSM 里注册 cloudflared 服务（临时域名版）
nssm install CloudflareTunnel "C:\Windows\System32\cloudflared.exe" tunnel --url http://127.0.0.1:8765
nssm set CloudflareTunnel DisplayName "Cloudflare Tunnel (Crypto Arb)"
nssm set CloudflareTunnel Start SERVICE_AUTO_START
nssm set CloudflareTunnel AppStdout C:\apps\crypto-arbitrage-dashboard\logs\tunnel.log
nssm set CloudflareTunnel AppStderr C:\apps\crypto-arbitrage-dashboard\logs\tunnel.log
nssm set CloudflareTunnel AppRotateFiles 1
nssm set CloudflareTunnel AppRotateBytes 10485760
nssm set CloudflareTunnel DependOnService ArbDashboard

nssm start CloudflareTunnel

# 拿到 trycloudflare 地址
Start-Sleep -Seconds 6
Select-String -Path C:\apps\crypto-arbitrage-dashboard\logs\tunnel.log -Pattern "trycloudflare.com" | Select-Object -Last 1
```

记下输出的 `https://xxxx.trycloudflare.com`，这就是你的公网访问地址。

---

## 第 6 步：自检

打开浏览器，访问 trycloudflare URL：

1. 弹出 Basic Auth 登录框 → 输入 .env 里的 `AUTH_USER` / `AUTH_PASS`
2. 看到面板，右上角"已连接"绿色徽章
3. 顶部能看到"共 6000+ tickers"

到这就完事了。

---

## 日常运维

```powershell
# 看服务状态
nssm status ArbDashboard
nssm status CloudflareTunnel

# 重启服务（比如改了 .env）
nssm restart ArbDashboard

# 跟踪日志
Get-Content C:\apps\crypto-arbitrage-dashboard\logs\stderr.log -Wait -Tail 30

# 更新代码
cd C:\apps\crypto-arbitrage-dashboard
git pull
pip install -r requirements.txt
nssm restart ArbDashboard

# 卸载服务（不要了的时候）
nssm stop ArbDashboard
nssm remove ArbDashboard confirm
nssm stop CloudflareTunnel
nssm remove CloudflareTunnel confirm
```

---

## 进阶：固定域名（可选）

`trycloudflare.com` 临时地址每次 cloudflared 重启会变。要固定域名，需要：

1. 在 [Cloudflare](https://dash.cloudflare.com/) 加自有域名（或转入）
2. `cloudflared tunnel login` 完成 OAuth
3. `cloudflared tunnel create arb` 创建命名 tunnel
4. 在 Cloudflare DNS 加一条 CNAME：`arb.yourdomain.com → <tunnel-id>.cfargotunnel.com`
5. 写一个 `config.yml` 关联 tunnel 和服务
6. `cloudflared tunnel run arb` 启动

详细步骤见 <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/>

---

## 故障排查

| 现象 | 解决 |
| --- | --- |
| `nssm install` 报"未识别的命令" | 关闭 PowerShell 再开一次让 PATH 生效 |
| 服务启动后立即停止 | 看 `stderr.log`，最常见原因：端口被占用 / .env 格式错 |
| 面板能开但没数据 | 服务器和交易所的连接被防火墙拦了，看日志里的 ccxt 报错 |
| trycloudflare URL 502 | uvicorn 服务没起来，先 `nssm status ArbDashboard` 检查 |
| 浏览器一直弹密码框 | .env 的 `AUTH_PASS` 含特殊字符建议改简单的或加引号 |
| 改了 .env 不生效 | 必须 `nssm restart ArbDashboard` 才会重新读 .env |

---

## 安全提醒

- ✅ Basic Auth 密码至少 16 位，混合大小写+数字+符号
- ✅ Cloudflare Tunnel 屏蔽了你服务器的真实 IP
- ✅ .env 不会被 git 提交（已 gitignore）
- ⚠ 服务器本身的 Windows Update / RDP 端口请单独加固
- ⚠ 如果你的密码很容易猜，Basic Auth 等于没有 —— 暴力破解 4 位数字 1 秒搞定
