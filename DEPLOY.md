# Windows Server Deployment Guide

> [中文](DEPLOY.zh.md) · English

Architecture:

```
  Public users
       │  HTTPS
       ▼
  Cloudflare Tunnel  (cloudflared.exe as a Windows service)
       │  HTTP, loopback only
       ▼
  uvicorn (Windows service via NSSM, listens on 127.0.0.1:8765)
       │  Basic Auth middleware (creds from .env)
       ▼
  ccxt → Binance / OKX / Bybit / Gate / Bitget
```

Why this stack:
- **Cloudflare Tunnel** — free, automatic HTTPS, hides your server's real
  IP, no need to open ports on the host.
- **NSSM** — registers uvicorn as a Windows service: auto-start on boot,
  auto-restart on crash.
- **Basic Auth** — once the service is public, any unauthenticated visitor
  can see it. The middleware enforces credentials at the app layer, covering
  HTTP and WebSocket.

---

## Prerequisites

- Windows Server 2019+ or Windows 10/11 Pro
- Python 3.10+ and git already installed
- Logged in over RDP
- All commands run in **PowerShell as Administrator**

```powershell
python --version    # >= 3.10
git --version
```

---

## 1. Clone the code

```powershell
mkdir C:\apps -Force
cd C:\apps
git clone https://github.com/renzhonghua8/crypto-arbitrage-dashboard.git
cd crypto-arbitrage-dashboard

pip install -r requirements.txt
```

---

## 2. Configure .env

```powershell
Copy-Item .env.example .env
notepad .env
```

Edit (**use a strong password**):

```env
# Direct connectivity from JP IP — no proxy needed
HTTPS_PROXY=
HTTP_PROXY=

# Basic Auth — pick a strong password (16+ chars)
AUTH_USER=admin
AUTH_PASS=replace-with-a-strong-password
```

---

## 3. Smoke test locally

```powershell
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8765
```

In another PowerShell:

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8765/
curl.exe -s -o NUL -w "%{http_code}`n" -u admin:yourpass http://127.0.0.1:8765/
```

Expected: `401` then `200`. Ctrl+C the running uvicorn.

---

## 4. Install NSSM and register uvicorn as a service

```powershell
winget install --id NSSM.NSSM -e --silent

$pythonPath = (Get-Command python).Source

nssm install ArbDashboard $pythonPath -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8765
nssm set ArbDashboard AppDirectory C:\apps\crypto-arbitrage-dashboard
nssm set ArbDashboard DisplayName "Crypto Arbitrage Dashboard"
nssm set ArbDashboard Description "Real-time crypto arbitrage scanner"
nssm set ArbDashboard Start SERVICE_AUTO_START
nssm set ArbDashboard AppStdout C:\apps\crypto-arbitrage-dashboard\logs\stdout.log
nssm set ArbDashboard AppStderr C:\apps\crypto-arbitrage-dashboard\logs\stderr.log
nssm set ArbDashboard AppRotateFiles 1
nssm set ArbDashboard AppRotateBytes 10485760

mkdir C:\apps\crypto-arbitrage-dashboard\logs -Force
nssm start ArbDashboard

nssm status ArbDashboard
Get-Content C:\apps\crypto-arbitrage-dashboard\logs\stderr.log -Wait -Tail 20
```

Wait until you see `snapshot: 6305 tickers | funding=200 ...` then Ctrl+C the log tail.

Verify:

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" -u admin:yourpass http://127.0.0.1:8765/api/snapshot
# Expected: 200
```

---

## 5. Cloudflare Tunnel (public HTTPS)

### 5.1 Install cloudflared

```powershell
Invoke-WebRequest `
  -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
  -OutFile "C:\Windows\System32\cloudflared.exe"

cloudflared --version
```

### 5.2 Try a quick tunnel (gets you a *.trycloudflare.com URL in 5s)

```powershell
cloudflared tunnel --url http://127.0.0.1:8765
```

Look for the `https://*.trycloudflare.com` URL in the output. Open it in
your browser — you'll be prompted for Basic Auth credentials.

Ctrl+C to stop, then register as a service.

### 5.3 Register cloudflared as a Windows service

> ⚠ The `trycloudflare.com` URL changes every time cloudflared restarts.
> See the bottom of this doc for setting up a permanent named tunnel with
> your own domain.

```powershell
nssm install CloudflareTunnel "C:\Windows\System32\cloudflared.exe" tunnel --url http://127.0.0.1:8765
nssm set CloudflareTunnel DisplayName "Cloudflare Tunnel (Crypto Arb)"
nssm set CloudflareTunnel Start SERVICE_AUTO_START
nssm set CloudflareTunnel AppStdout C:\apps\crypto-arbitrage-dashboard\logs\tunnel.log
nssm set CloudflareTunnel AppStderr C:\apps\crypto-arbitrage-dashboard\logs\tunnel.log
nssm set CloudflareTunnel AppRotateFiles 1
nssm set CloudflareTunnel AppRotateBytes 10485760
nssm set CloudflareTunnel DependOnService ArbDashboard

nssm start CloudflareTunnel

Start-Sleep -Seconds 6
Select-String -Path C:\apps\crypto-arbitrage-dashboard\logs\tunnel.log -Pattern "trycloudflare.com" | Select-Object -Last 1
```

The matched line shows your public URL.

---

## 6. Self-check

Open the trycloudflare URL in your browser:

1. Basic Auth prompt → enter `AUTH_USER` / `AUTH_PASS`
2. Dashboard loads, green "Connected" badge top-right
3. Header shows "6000+ tickers"

Done.

---

## Day-to-day operations

```powershell
# Status
nssm status ArbDashboard
nssm status CloudflareTunnel

# Restart (after editing .env)
nssm restart ArbDashboard

# Tail logs
Get-Content C:\apps\crypto-arbitrage-dashboard\logs\stderr.log -Wait -Tail 30

# Update code
cd C:\apps\crypto-arbitrage-dashboard
git pull
pip install -r requirements.txt
nssm restart ArbDashboard

# Uninstall
nssm stop ArbDashboard
nssm remove ArbDashboard confirm
nssm stop CloudflareTunnel
nssm remove CloudflareTunnel confirm
```

---

## Advanced: permanent named tunnel (optional)

The `trycloudflare.com` URL rotates on every restart. For a stable hostname:

1. Add a domain to [Cloudflare](https://dash.cloudflare.com/) (or transfer one in)
2. `cloudflared tunnel login` — completes browser OAuth
3. `cloudflared tunnel create arb`
4. In Cloudflare DNS, add `CNAME arb → <tunnel-id>.cfargotunnel.com`
5. Write a `config.yml`:
   ```yaml
   tunnel: <tunnel-id>
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel-id>.json
   ingress:
     - hostname: arb.yourdomain.com
       service: http://127.0.0.1:8765
     - service: http_status:404
   ```
6. `cloudflared tunnel run arb`
7. Switch the NSSM service to use `tunnel run arb` instead of `--url`.

Full reference: <https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/>

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `nssm install` "command not found" | Restart PowerShell so PATH picks up the new install |
| Service starts then dies immediately | Check `stderr.log`. Usually port-in-use or malformed `.env`. |
| Dashboard loads, no data | Server can't reach exchanges — check `stderr.log` for ccxt errors. |
| trycloudflare URL returns 502 | uvicorn isn't up. Check `nssm status ArbDashboard`. |
| Browser keeps prompting for password | Special chars in `AUTH_PASS` — simplify or quote properly. |
| Changed `.env` but nothing happens | Must `nssm restart ArbDashboard` to reload env. |

---

## Security notes

- ✅ Basic Auth password: 16+ chars, mixed case + digits + symbols
- ✅ Cloudflare Tunnel hides your origin IP
- ✅ `.env` is gitignored — your password never reaches the repo
- ⚠ Harden the host itself: RDP port, Windows Update, etc.
- ⚠ A weak Basic Auth password is no defense — 4-digit PINs crack in seconds
