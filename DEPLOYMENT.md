# BeatNow Backend — Beta técnica

Auditoría de seguridad para productores y checklist de corte de audio: [SECURITY_BETA.md](SECURITY_BETA.md). La revisión no modifica el VPS ni inspecciona los vhosts reales.

## Arquitectura

FastAPI mantiene MongoDB como base de datos y escribe multimedia mediante `StorageProvider`. La implementación actual, `LocalStorageProvider`, usa claves relativas bajo `MEDIA_ROOT`; Nginx sirve esos archivos mediante `MEDIA_BASE_URL`.

```text
FastAPI -> MongoDB Atlas
        -> StorageProvider -> LocalStorageProvider -> /srv/beatnow/media
                                                   -> Nginx -> https://res.beatnow.app
```

Las portadas, avatares y previews son servidos por el host de recursos. Los masters no tienen endpoint HTTP y viven fuera de `MEDIA_ROOT`. `/beatnow/{path}` solo redirige los prefijos públicos permitidos (`avatars/`, `covers/`, `previews/`).

## Variables de entorno

Obligatorias o recomendadas para producción:

```env
ENVIRONMENT=production
SECRET_KEY=<secreto aleatorio de al menos 32 caracteres>
MONGO_URI=<URI MongoDB Atlas>
MONGO_DB=BeatNow
PUBLIC_BASE_URL=https://api.beatnow.app
APP_WEB_BASE_URL=https://app.beatnow.app
MEDIA_ROOT=/srv/beatnow/media
MEDIA_BASE_URL=https://res.beatnow.app
STORAGE_PROVIDER=local
MAX_IMAGE_UPLOAD_SIZE=10485760
MAX_AUDIO_UPLOAD_SIZE=52428800
MAX_IMAGE_DIMENSION=4096
MAX_IMAGE_PIXELS=40000000
TRUSTED_PROXY_IPS=127.0.0.1,::1
TRUSTED_HOSTS=api.beatnow.app,localhost,127.0.0.1
BEATS_ORIGINAL_DIR=/srv/beatnow/private/beats/originals
BEATS_PREVIEW_DIR=/srv/beatnow/media/previews
FFMPEG_BINARY=ffmpeg
FFPROBE_BINARY=ffprobe
PREVIEW_DURATION_SECONDS=30
PREVIEW_FORMAT=mp3
PREVIEW_AUDIO_BITRATE=96k
BEAT_PROCESSING_TIMEOUT_SECONDS=120
BEAT_WATERMARK_ENABLED=true
BEAT_WATERMARK_FILE=/srv/beatnow/private/audio-tags/beatnow-tag.wav
BEAT_WATERMARK_INTERVAL_SECONDS=30
RIGHTS_CONFIRMATION_VERSION=beta-1
LOGIN_RATE_LIMIT=5
REGISTER_RATE_LIMIT=3
RESET_RATE_LIMIT=3
CONFIRMATION_RATE_LIMIT=5
CONFIRMATION_MAX_ATTEMPTS=5
```

Completar también `SMTP_*`, `EMAIL_SENDER`, expiraciones JWT y `CORS_ORIGINS` según `.env.example`. No guardar el fichero de entorno en Git.

## Dependencias

Producción no instala pytest:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-production.txt
python -m pip check
python -c "from main import app"
```

Desarrollo y CI usan `requirements-dev.txt`.

## Migración multimedia legacy

La migración nunca borra el origen, es reejecutable y funciona en dry-run por defecto. Debe ejecutarse con el mismo entorno de producción y acceso a MongoDB.

Dry-run:

```bash
cd /opt/beatnow-back
. .venv/bin/activate
python scripts/migrate_media_v2.py --legacy-root /var/www/html/beatnow
```

Ejecución real, solo después de revisar el dry-run y disponer de backup:

```bash
python scripts/migrate_media_v2.py --legacy-root /var/www/html/beatnow --execute
```

Para limitar una prueba:

```bash
python scripts/migrate_media_v2.py --legacy-root /var/www/html/beatnow --user-id <OBJECT_ID>
```

No eliminar el árbol legacy hasta comprobar MongoDB, URLs públicas, audio Range y backups.

## Compatibilidad API

- Los endpoints `/v1/api/posts/*` conservan el nombre `posts`; la media pública nueva vive en `covers/<post_id>/` y `previews/<post_id>/`.
- `cover_image_url`, `audio_url`, `isLiked`, `isSaved` y `_id` se mantienen. `audio_url` y `preview_url` son la misma URL de preview.
- `caratula` continúa como alias de respuesta de `cover_image_url`; no existe un segundo archivo físico.
- El avatar devuelve `profile_image_url` y el alias `photo_profile` en las operaciones de cambio/borrado.
- Tokens JWT nuevos usan `sub=user_id`; el backend acepta temporalmente tokens access/refresh legacy con `sub=username`.
- `/token` y `/v1/api/users/login` se mantienen y comparten la misma lógica.
- `/beatnow/{path}` se conserva temporalmente como redirección `308` a `MEDIA_BASE_URL`.
- Listados aceptan `limit` y `skip`; defaults actuales preservan respuestas tipo lista con un máximo seguro.

## Acciones manuales — VPS

Estas acciones no las realiza el código:

1. Configurar las variables anteriores en el `EnvironmentFile` de `beatnow.service`.
2. Configurar `BEATS_ORIGINAL_DIR` fuera de cualquier raíz pública (sugerido `/srv/beatnow/private/beats/originals`) y `BEATS_PREVIEW_DIR` dentro de `/srv/beatnow/media`.
3. Verificar permisos: `beatnow` escribe en media y originals; Nginx solo necesita leer media y no debe leer masters.
4. Crear/verificar `avatars`, `covers`, `previews` y `temp` bajo `/srv/beatnow/media`, además del directorio privado.
5. Instalar `requirements-production.txt`, paquetes de sistema `ffmpeg`/`ffprobe` y ejecutar smoke test.
6. Migrar medios legacy con `scripts/migrate_media_v2.py` cuando aplique; para masters públicos actuales usar `scripts/migrate_beat_masters.py`. Hacer backup y revisar dry-run antes de `--execute`.
7. Bloquear todo prefijo legacy `beats/`, purgar caché/CDN antigua y comprobar que no existe ninguna ruta de descarga de masters.
8. Reiniciar `beatnow.service` y validar `/healthz`, `/readyz`, login, avatar, upload/update/delete y reproducción Range.
9. Rotar cualquier secreto que haya podido existir históricamente en Git; el código no puede confirmar ni ejecutar esa rotación externa.

## Acciones manuales — MongoDB

`ensure_indexes()` solicita al arrancar índices únicos para username/email, follows e interactions; TTL para `MailCode.expires_at`, `PasswordReset.expires_at` y `RefreshTokens.expires_at`; y búsquedas por `RefreshTokens.user_id`. Verificar en Atlas que fueron creados correctamente y resolver duplicados legacy si un índice único falla.

Activar y verificar backups de MongoDB Atlas. Probar una restauración antes de la beta.

## Acciones manuales — Nginx

Mantener `res.beatnow.app` apuntando a `/srv/beatnow/media`. Además:

- `server_tokens off;`
- `client_max_body_size` ligeramente superior a `MAX_AUDIO_UPLOAD_SIZE` (la revisión sugiere `55m` para un máximo de 50 MiB).
- Range habilitado para audio, tipos MIME correctos y cache largo para recursos versionables.
- Bloquear `/metrics` públicamente o permitirlo solo desde localhost/red de monitoring.
- Aplicar `limit_req` a login, registro, confirmación y reset; el limiter en proceso es una segunda barrera para un único worker, no un limiter distribuido.
- Sobrescribir, no concatenar ciegamente, `X-Forwarded-For`; el backend solo confía en `TRUSTED_PROXY_IPS`.
- Propagar `X-Request-ID` y configurar timeouts de proxy/upload apropiados.
- Permitir únicamente los prefijos públicos necesarios (`/avatars/`, `/covers/`, `/previews/`) y devolver 404 para `/beats/`, `/private/`, extensiones/rutas de masters y dotfiles.
- Mantener `BEATS_ORIGINAL_DIR` fuera de todo `root`/`alias` público y sin ninguna location Nginx, ni siquiera `internal`, durante esta fase.

Ejecutar `nginx -t` antes de recargar. Nginx ya es responsable de servir media; Python no debe montar ese directorio públicamente.

## Acciones manuales — systemd

Bloque de hardening recomendado para `beatnow.service`:

```ini
[Service]
User=beatnow
Group=beatnow
WorkingDirectory=/opt/beatnow-back
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/beatnow/media /srv/beatnow/.beat-analysis-tmp /srv/beatnow/private/beats/originals
UMask=0027
```

Con `ProtectSystem=strict`, `ReadWritePaths` debe permitir `/srv/beatnow/media` y `/srv/beatnow/.beat-analysis-tmp` para que la API gestione los análisis privados. Revisar con `systemd-analyze security beatnow.service` y probar uploads antes de mantener el cambio.

## Backups

Se necesitan dos políticas independientes:

- backup y restauración probada de MongoDB Atlas;
- backup externo de `/srv/beatnow/media`, fuera del mismo VPS.

No considerar una copia en el propio VPS como backup suficiente.

## Cambios futuros de clientes

Web, app-web y Flutter deben migrar gradualmente de `caratula` a `cover_image_url`, dejar de construir URLs a partir de formatos/rutas legacy y tratar las URLs de API como opacas. También deben usar `/logout`, rotar refresh tokens en cada refresh y aceptar paginación `limit`/`skip`. Tras completar esa migración se podrán retirar `caratula`, subjects JWT por username y `/beatnow/{path}`.
