# BeatNow Backend

Backend API built with FastAPI and MongoDB.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-production.txt
cp .env.example .env
uvicorn main:app --reload
```

## Production deployment (Ubuntu VPS)

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
