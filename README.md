# BeatNow Backend

Backend API built with FastAPI and MongoDB.

Deployment guide: see [DEPLOYMENT.md](DEPLOYMENT.md).
Launch plan: see [LAUNCH_ROADMAP.md](LAUNCH_ROADMAP.md).

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn main:app --reload
```

`ENABLE_CHANGE_STREAM_SYNC` is disabled by default. Enable it only if your MongoDB deployment supports change streams and you explicitly want counter reconciliation in the background.

## Production deployment (Ubuntu VPS)

## MongoDB configuration

You can configure MongoDB in **either** of these ways:

### Option A: full URI

```env
MONGO_URI=mongodb+srv://user:password@cluster0.example.mongodb.net/BeatNow?retryWrites=true&w=majority
MONGO_DB=BeatNow
```

### Option B: separate variables

```env
MONGO_USER=your_mongo_user
MONGO_PASSWORD=your_mongo_password
MONGO_HOST=cluster0.example.mongodb.net
MONGO_DB=BeatNow
```

`MONGO_URI` takes precedence if it is set.

Production runs Uvicorn on `127.0.0.1:8001` behind Nginx. Public media is written through `StorageProvider` and served only by Nginx from `https://res.beatnow.app`.

```bash
uvicorn main:app --host 127.0.0.1 --port 8001
```

Recommended systemd service:

```ini
[Unit]
Description=BeatNow API
After=network.target

[Service]
User=beatnow
Group=beatnow
WorkingDirectory=/opt/beatnow-back
EnvironmentFile=/etc/beatnow/api.env
ExecStart=/opt/beatnow-back/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/beatnow/media
UMask=0027

[Install]
WantedBy=multi-user.target
```

Nginx should terminate TLS, proxy to `127.0.0.1:8001`, enforce request size limits and rate limiting on auth routes.
