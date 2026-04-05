# BamDude — Installation from dev branch on Ubuntu

Step-by-step guide for installing BamDude from the `dev` branch on Ubuntu (22.04/24.04), running as a systemd service, and updating.

---

## 1. System dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git curl ffmpeg nodejs npm
```

Check versions:

```bash
python3 --version   # 3.10+
node --version      # 20+
npm --version       # 9+
```

If Node.js is too old (< 20), install from NodeSource:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

---

## 2. Clone repository

> **Note:** The `main` branch is currently empty. All active development is on `dev`. Clone with `-b dev`:

```bash
cd /opt
sudo git clone -b dev https://github.com/kainpl/bamdude.git
sudo chown -R $USER:$USER /opt/bamdude
cd /opt/bamdude
```

---

## 3. Backend setup (Python venv)

```bash
cd /opt/bamdude

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

Test that it starts:

```bash
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
# Ctrl+C to stop
```

---

## 4. Frontend build

```bash
cd /opt/bamdude/frontend

# Install dependencies
npm ci

# Build — output goes to /opt/bamdude/static/
npm run build
```

The backend serves the built frontend from `static/` automatically. No separate frontend server needed.

---

## 5. Create data directories and .env

```bash
mkdir -p /opt/bamdude/data /opt/bamdude/logs

# Create .env so the app stores DB and logs in subdirectories (not project root)
cat > /opt/bamdude/.env << 'EOF'
DATA_DIR=./data
LOG_DIR=./logs
DEBUG=false
EOF
```

Without `DATA_DIR`, the database (`bambuddy.db`) is created in the project root. With it, everything goes into `./data/`.

---

## 6. Configure as systemd service

Create the service file:

```bash
sudo nano /etc/systemd/system/bamdude.service
```

Paste:

```ini
[Unit]
Description=BamDude - 3D Printer Management
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
Group=YOUR_USERNAME
WorkingDirectory=/opt/bamdude
Environment="PATH=/opt/bamdude/venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/opt/bamdude/.env
ExecStart=/opt/bamdude/venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Replace `YOUR_USERNAME` with your actual username (e.g., `ubuntu`, `bamdude`).

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable bamdude
sudo systemctl start bamdude
```

Check status:

```bash
sudo systemctl status bamdude
```

View logs:

```bash
sudo journalctl -u bamdude -f
```

---

## 7. Open in browser

Go to **http://YOUR_SERVER_IP:8000** and add your first printer.

---

## 8. Telegram bot setup

1. Create a bot via [@BotFather](https://t.me/BotFather) in Telegram, copy the token
2. In BamDude Settings > Notifications > Add Provider > Telegram, paste the bot token
3. Enable Registration Mode in the Telegram Chats section
4. Send `/start` to the bot from your Telegram
5. Back in Settings, your chat appears as "pending" — assign a role (e.g., Administrators), enable it
6. Disable Registration Mode
7. Done! Use reply keyboard or inline menus to control printers

---

## 9. Updating from dev branch

When you want to pull new changes:

```bash
cd /opt/bamdude

# Stop the service
sudo systemctl stop bamdude

# Pull latest changes
git pull origin dev

# Update backend dependencies (if requirements.txt changed)
source venv/bin/activate
pip install -r requirements.txt

# Rebuild frontend (if frontend/ changed)
cd frontend
npm ci
npm run build
cd ..

# Restart the service
sudo systemctl start bamdude
```

### Quick update script

Create `/opt/bamdude/update.sh`:

```bash
#!/bin/bash
set -e

cd /opt/bamdude
echo "Stopping service..."
sudo systemctl stop bamdude

echo "Pulling latest changes..."
git pull origin dev

echo "Updating Python dependencies..."
source venv/bin/activate
pip install -r requirements.txt

echo "Rebuilding frontend..."
cd frontend
npm ci
npm run build
cd ..

echo "Starting service..."
sudo systemctl start bamdude

echo "Done! Version:"
grep APP_VERSION backend/app/core/config.py
```

```bash
chmod +x /opt/bamdude/update.sh
```

Run update:

```bash
/opt/bamdude/update.sh
```

---

## 10. When do you need to rebuild what?

| What changed | Action needed |
|---|---|
| `backend/**/*.py` | Just restart service: `sudo systemctl restart bamdude` |
| `requirements.txt` | `source venv/bin/activate && pip install -r requirements.txt` + restart |
| `frontend/src/**` | `cd frontend && npm run build` + restart |
| `frontend/package.json` | `cd frontend && npm ci && npm run build` + restart |
| `backend/app/data/*.json` | Just restart service (i18n files loaded at startup) |
| Database model changes | Just restart (auto-migrated via `create_all` + `run_migrations`) |

**Rule of thumb:** backend Python changes need only a restart. Frontend changes need `npm run build`. Dependency changes need `pip install` or `npm ci`.

---

## 11. Troubleshooting

### Service won't start

```bash
# Check logs
sudo journalctl -u bamdude -n 50 --no-pager

# Try running manually
cd /opt/bamdude
source venv/bin/activate
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
```

### Port already in use

```bash
sudo lsof -i :8000
# Kill the process or change PORT in the service file
```

### Permission errors

```bash
sudo chown -R $USER:$USER /opt/bamdude
```

### Frontend shows old version after update

```bash
cd /opt/bamdude/frontend
rm -rf node_modules
npm ci
npm run build
sudo systemctl restart bamdude
```

### Database issues after update

The database auto-migrates on startup (`Base.metadata.create_all` + `run_migrations`). If something is wrong:

```bash
# Backup first!
cp /opt/bamdude/data/bambuddy.db /opt/bamdude/data/bambuddy.db.bak

# Restart — migrations run on startup
sudo systemctl restart bamdude
```

---

## 12. Optional: Nginx reverse proxy with HTTPS

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
```

Create config:

```bash
sudo nano /etc/nginx/sites-available/bamdude
```

```nginx
server {
    listen 80;
    server_name bamdude.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
        client_max_body_size 500M;
    }
}
```

Enable and get SSL:

```bash
sudo ln -s /etc/nginx/sites-available/bamdude /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d bamdude.yourdomain.com
```

> **Important:** `proxy_read_timeout 86400` and WebSocket headers are required for real-time printer updates. `client_max_body_size 500M` allows large 3MF uploads.
