# BeatNow Backend

Backend API built with FastAPI and MongoDB.

Deployment guide: see [DEPLOYMENT.md](/Users/hugogarcia/projects/beatnow/back/DEPLOYMENT.md).
Launch plan: see [LAUNCH_ROADMAP.md](/Users/hugogarcia/projects/beatnow/back/LAUNCH_ROADMAP.md).

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-production.txt
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

Run behind **Nginx** and **Gunicorn/Uvicorn workers**.

```bash
gunicorn main:app \
  -k uvicorn.workers.UvicornWorker \
  --workers 4 \
  --bind 127.0.0.1:8001 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
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
ExecStart=/opt/beatnow-back/.venv/bin/gunicorn main:app -k uvicorn.workers.UvicornWorker --workers 4 --bind 127.0.0.1:8001 --timeout 60 --access-logfile - --error-logfile -
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Nginx should terminate TLS, proxy to `127.0.0.1:8001`, enforce request size limits and rate limiting on auth routes.
