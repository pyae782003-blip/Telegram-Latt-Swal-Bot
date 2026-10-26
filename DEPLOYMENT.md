# 🚀 Cloud Deployment Guide (Render & Railway)

This project is configured to run smoothly as a high-availability, multi-tenant background worker on cloud platforms like **Render** and **Railway**.

---

## 🌟 Quick Feature Highlights for Cloud Deployment
1. **Auto-Reconnection**: Configured with `bootstrap_retries=-1` and auto-reconnecting background supervisors so the bot never crashes on network fluctuations.
2. **Persistent Storage Support**: Database path is configurable via `DATABASE_PATH` or `DATABASE_URL` so your SQLite data persists across cloud container restarts and redeployments.
3. **Dual-Mode Execution (Worker & Web)**:
   - Default: Runs as a **Background Worker**.
   - If deployed as a **Web Service** where the platform injects a `$PORT`, an integrated lightweight HTTP health check server automatically responds with `200 OK` on `/health` and `/` to satisfy health check probes.
4. **Multi-Tenant Bot Supervisor**: Automatically launches, monitors, and recovers all tenant store bots dynamically in the background.

---

## 🛠️ Option 1: Deploy on Render

### Step 1: Push Code to GitHub / GitLab
Make sure your repository has:
- `bot.py`
- `database.py`
- `requirements.txt`
- `Procfile`
- `render.yaml`

### Step 2: Create a Background Worker on Render
1. Log in to [Render Dashboard](https://dashboard.render.com).
2. Click **New +** -> **Background Worker**.
3. Connect your GitHub repository.
4. Configure the settings:
   - **Name**: `telegram-multitenant-bot`
   - **Region**: `Singapore` (or closest to Myanmar/Thailand)
   - **Branch**: `main`
   - **Runtime**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
5. **Add Persistent Disk (Crucial for SQLite)**:
   - Scroll down to **Disks**.
   - Click **Add Disk**.
   - **Name**: `bot-data`
   - **Mount Path**: `/var/data`
   - **Size**: `1 GB` (or more as needed).
6. **Set Environment Variables**:
   Under **Environment Variables**, add:
   - `TELEGRAM_BOT_TOKEN`: `<Your-Main-Telegram-Bot-Token>`
   - `SAAS_SUPER_ADMIN_IDS`: `1920334929`
   - `DATABASE_PATH`: `/var/data/store.db`
   - `PYTHONUNBUFFERED`: `1`
7. Click **Create Background Worker**.

*(Tip: You can also use Render Blueprint by selecting "New Blueprint" and choosing `render.yaml` for 1-click setup!)*

---

## 🚂 Option 2: Deploy on Railway

### Step 1: Deploy from GitHub Repo
1. Log in to [Railway](https://railway.app).
2. Click **New Project** -> **Deploy from GitHub repo**.
3. Select this repository.

### Step 2: Configure Volume & Variables
1. Railway automatically detects `railway.json` and `Procfile`.
2. Add a Volume for persistent SQLite storage:
   - Click your service in Railway.
   - Go to **Data** / **Volumes** -> **Add Volume**.
   - Mount path: `/data` (or `/var/data`)
3. In **Variables**, add:
   - `TELEGRAM_BOT_TOKEN`: `<Your-Main-Telegram-Bot-Token>`
   - `SAAS_SUPER_ADMIN_IDS`: `1920334929`
   - `DATABASE_PATH`: `/data/store.db`
   - `PYTHONUNBUFFERED`: `1`
4. Railway will automatically deploy and restart if needed.

---

## 🪰 Option 3: Deploy on Fly.io (Recommended for Ultra-low Latency)

### Step 1: Install Flyctl and Log in
```bash
fly auth login
```

### Step 2: Launch App & Create Persistent Volume
Run `fly launch` in the project root:
```bash
fly launch --no-deploy
```

Create a persistent volume for the SQLite database (mounts to `/data`):
```bash
fly volumes create store_data --size 1 --region sin
```

### Step 3: Set Secrets & Deploy
Set your required Telegram bot secrets and Cloud Database URL:
```bash
fly secrets set TELEGRAM_BOT_TOKEN="<Your-Master-Bot-Token>" SAAS_SUPER_ADMIN_IDS="1920334929" DATABASE_URL="postgresql://postgres.[REF]:[PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres"
```

Deploy the app:
```bash
fly deploy
```
*Note: When using Supabase / PostgreSQL, no persistent volume is needed. If you prefer SQLite, create a volume: `fly volumes create store_data --size 1 --region sin` and uncomment `[mounts]` in `fly.toml`.*

---

## ☁️ Option 4: Deploy on Koyeb

### Step 1: Create Service on Koyeb
1. Log in to [Koyeb Console](https://app.koyeb.com).
2. Click **Create App** -> **GitHub**.
3. Select this repository.
4. Set **Builder** to **Dockerfile**.

### Step 2: Set Environment Variables
Add the following in the **Environment Variables** section:
- `TELEGRAM_BOT_TOKEN`: `<Your-Master-Bot-Token>`
- `SAAS_SUPER_ADMIN_IDS`: `1920334929`
- `DATABASE_URL`: `postgresql://postgres.[REF]:[PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres` (from Supabase Project Settings -> Database -> Connection string)
- `PYTHONUNBUFFERED`: `1`
- `PORT`: `8080` (Koyeb exposes an HTTP port)

### Step 3: Deploy
Click **Deploy**. The integrated healthcheck server automatically responds with `200 OK` on port 8080 while all tenant bots run in the background.

---

## 📋 Environment Variables Reference

| Variable | Required | Description | Default |
| :--- | :---: | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | **Yes** | Master bot token from @BotFather | Pre-configured |
| `SAAS_SUPER_ADMIN_IDS` | **Yes** | SaaS Super Admin user ID | `1920334929` |
| `DATABASE_PATH` | Recommended | Filepath for SQLite DB | `/var/data/store.db` |
| `STORE_NAME` | No | Name for master store #1 | `Latt Swal Store` |
| `ADMIN_USERNAME` | No | Master admin username | `latt_swal2026` |
| `KPAY_PHONE` | No | KPay receiver phone | `09758511925` |
| `WPAY_PHONE` | No | WavePay receiver phone | `09779737113` |
| `PROMPTPAY_PHONE` | No | PromptPay phone number | `09758511925` |
| `THAI_BANK_INFO` | No | Thai Bank details | Kasikorn Bank... |
| `PYTHONUNBUFFERED` | No | Real-time logging | `1` |
