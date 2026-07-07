# BeatNow Backend Deployment

## Requisitos

- Ubuntu 22.04 LTS
- Python 3.11
- Nginx
- systemd
- acceso a MongoDB Atlas

## Estructura recomendada

- código: `/opt/beatnow-back`
- entorno: `/opt/beatnow-back/.venv`
- media: `/var/lib/beatnow/media`
- env: `/etc/beatnow/api.env`

## Instalación inicial

```bash
sudo adduser --system --group beatnow
sudo mkdir -p /opt/beatnow-back /var/lib/beatnow/media /etc/beatnow
sudo chown -R beatnow:beatnow /opt/beatnow-back /var/lib/beatnow/media
```

## Entorno

Partiendo de `.env.example`, crea `/etc/beatnow/api.env`.

Variables críticas:

- `ENVIRONMENT=production`
- `SECRET_KEY=<valor largo y aleatorio>`
- `MONGO_URI=<mongodb+srv://...>`
- `PUBLIC_BASE_URL=https://app.beatnow.app`
- `MEDIA_BASE_URL=https://res.beatnow.app/beatnow`
- `MEDIA_ROOT=/var/lib/beatnow/media`
- `SMTP_*`
- `EMAIL_SENDER`

## Dependencias

```bash
cd /opt/beatnow-back
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements-production.txt
```

## systemd

Archivo: `/etc/systemd/system/beatnow-api.service`

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

Activación:

```bash
sudo systemctl daemon-reload
sudo systemctl enable beatnow-api
sudo systemctl start beatnow-api
sudo systemctl status beatnow-api
```

## Nginx

Configura un server block para `api.beatnow.app`:

```nginx
server {
    server_name api.beatnow.app;

    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Despues:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## SSL

Recomendado con Let's Encrypt:

```bash
sudo certbot --nginx -d api.beatnow.app
```

## Verificación

```bash
curl -i https://api.beatnow.app/healthz
curl -i https://api.beatnow.app/readyz
```

## Operación

- logs app: `journalctl -u beatnow-api -f`
- logs nginx: `/var/log/nginx/access.log` y `error.log`
- reinicio manual: `sudo systemctl restart beatnow-api`
- despliegue CI: `back/.github/workflows/deploy_vps.yml`
