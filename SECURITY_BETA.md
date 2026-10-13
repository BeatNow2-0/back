# BeatNow backend: beta de productores

> La configuración vigente de protección de audio está en `PREVIEW_PROTECTION_STATUS.md`. No existe descarga de master en la fase 2; cualquier referencia histórica a esa descarga queda anulada.

Estado de esta revisión: cambios de backend implementados; **no desplegados ni verificados en el VPS**. El repositorio no contiene la configuración real de Nginx, systemd, firewall ni MongoDB Atlas.

## SECURITY AUDIT SUMMARY

### CRITICAL

- Antes de esta revisión, el audio original se guardaba como `beats/{post_id}/audio.{ext}` bajo `MEDIA_ROOT`, y `audio_url` apuntaba a `MEDIA_BASE_URL`. El dominio `res.beatnow.app` sirve ese recurso directamente. Cualquier persona con esa URL podía obtener el archivo completo. Los responses también incluían `audio_key`.
- `main.py` no monta `StaticFiles`, pero redirige `/beatnow/{path}` a la URL pública del storage. La configuración externa que sirve `res.beatnow.app` no está en el repositorio y debe inspeccionarse en el VPS.
- Los beats ya almacenados **siguen expuestos hasta ejecutar la migración y cerrar en Nginx las rutas de audio antiguas**. Desplegar solamente el código nuevo no los protege.

### HIGH

- La versión anterior fijaba FastAPI `0.110.1` y Starlette `0.37.2`; esa versión de Starlette es vulnerable a consumo excesivo de recursos al procesar multipart con muchos campos. Se actualizaron las dependencias fijadas a FastAPI `0.142.2` y Starlette `1.7.0`. Referencia: [GHSA-f96h-pmfr-66vw](https://github.com/advisories/GHSA-f96h-pmfr-66vw).
- Se mantienen ownership checks en edición y borrado de beats. No existe endpoint de descarga del original, ni siquiera para el propietario.
- Antes, uploads de beat aceptaban imágenes/audio con límites y validación por formato parcial, pero el original se publicaba sin transcodificar. La ruta nueva verifica contenedor, pista de audio y duración con `ffprobe`, genera preview con FFmpeg y limita bytes/tiempo.
- La autenticación ya usa JWT HS256, access token de 15 minutos, refresh token rotatorio y revocable, passwords con `bcrypt_sha256`/bcrypt y rate limits en login, registro y reset. No cambié el contrato de tokens.
- Los rate limits actuales son en memoria por proceso. No coordinan workers ni varios VPS; el limitador de `/stream` limita solicitudes a ese endpoint, mientras las URLs públicas de preview se sirven directamente desde el recurso/CDN.
- Se aplican además límites locales a uploads, búsquedas, reportes y stream; las URLs de preview directas quedan fuera del rate limit de FastAPI.
- Los reportes de copyright ahora se guardan como `open`; no existe panel/API administrativa de revisión. Ningún reporte borra un beat automáticamente.

### MEDIUM

- CORS de producción ya no incluye localhost por defecto; producción rechaza `*`, origins localhost, `DEBUG=true` y algoritmos JWT distintos de HS256. La documentación OpenAPI se desactiva en producción.
- Se añadió `TrustedHostMiddleware` de producción. `TRUSTED_HOSTS` debe incluir los nombres que realmente llegan al proceso después de Nginx.
- El logging JSON registra request ID/IP y eventos de beats/auth/reportes sin registrar JWT, cookies, audio ni URI Mongo. Nginx debe sobrescribir `X-Forwarded-For`; el backend solo lo interpreta cuando la IP peer está en `TRUSTED_PROXY_IPS`.
- No hay `StaticFiles` configurado. El APK se descarga públicamente mediante endpoints separados; no es audio de productor.
- `PROMETHEUS_ENABLED=true` expone `/metrics` sin autenticación en FastAPI. Mantenerlo desactivado o restringirlo en Nginx/firewall.
- La limpieza de análisis temporales es un script manual/cron y no está programada por el código.
- MongoDB usa Motor `3.4.0`, que está en proceso de retirada: MongoDB anunció su deprecación el 14-05-2026 y soporte de correcciones críticas hasta el 14-05-2027. No se migró el driver en esta revisión para evitar una migración transversal del acceso async a DB; planificarla antes de esa fecha. [Guía oficial de migración Motor → PyMongo Async](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/).

### LOW

- Los errores HTTP y no controlados devuelven mensajes genéricos y request ID. Algunas búsquedas convierten errores de base de datos en `500` genérico y los logs no incluyen cuerpos.
- Las fotos/portadas continúan públicas, y sus URLs/keys siguen expuestas como antes. No contienen el master de audio.
- SHA-256 identifica los bytes originales y permite verificar integridad; no acredita por sí solo derechos de autor ni titularidad.

## CHANGES IMPLEMENTED

- `services/beat_audio.py`: nombres internos aleatorios, hash SHA-256, allowlist WAV/MP3/FLAC/M4A, límite de tamaño/duración, `ffprobe`, preview FFmpeg, timeout y watermark opcional sobre preview.
- `services/storage.py`: conserva `audio_key`/`audio_url` como contrato del preview, guarda master fuera de `MEDIA_ROOT`, aplica/revierte master y preview juntos, elimina masters al borrar beat y filtra metadatos privados de serializadores.
- `routes/posts_routes.py`: requiere `rights_confirmed=true` al subir o reemplazar audio. Añade stream público de preview, límites de subida/stream y eventos de seguridad. Edición/borrado continúan sujetos a ownership; no hay descarga de master.
- `routes/beat_analysis_routes.py`: limita análisis y al publicar vuelve a procesar el archivo hacia master privado + preview; publicar sin confirmar derechos falla.
- `routes/copyright_routes.py`, `model/copyright_schemas.py`, `config/db.py`: endpoint de reporte anónimo con email, rate limit, estados iniciales y colección/índices. No implementa consola de moderación.
- `config/settings.py`, `main.py`: configuración de rutas, bitrate/formato, watermark, timeout, CORS/hosts de producción y documentación API desactivada.
- `requirements-production.txt`, `requirements-dev.txt`: FastAPI/Starlette/instrumentator/Uvicorn actualizados; cliente de pruebas actualizado para la versión actual de Starlette.
- `core/logging.py`, `core/request_context.py`, `config/security.py`, `routes/interactions_routes.py`: más trazabilidad, evento de login fallido y filtrado de campos internos de respuestas.
- `scripts/migrate_beat_masters.py`: dry-run por defecto para migrar el audio público existente a master privado y regenerar preview.
- `.env.example`: nuevas variables sin secretos reales.
- `tests/test_beat_audio_security.py` y tests existentes de flujos: cobertura del procesamiento/hash, validaciones, filtrado del payload y ausencia de ruta de master.

## AUDIO SECURITY ARCHITECTURE

`productor → POST /v1/api/posts/upload (multipart + rights_confirmed=true) → límite y validación de nombre/tipo/tamaño → master exacto en BEATS_ORIGINAL_DIR/{producer_id}/{uuid}.{ext} → ffprobe valida el contenido real/duración → FFmpeg produce BEATS_PREVIEW_DIR/{post_id}/preview.{mp3|m4a} → audio_key/audio_url apuntan solo al preview → feed, perfil y player`.

El master se escribe en staging privado con permisos restrictivos y se mueve al destino con `os.replace`; el preview se prepara en staging y se publica con el lote de media existente. Si falla procesamiento/DB, el lote revierte y limpia temporales. Los registros de beat se mantienen ocultos de feed/perfiles mientras `publication_status=processing`.

Los beats antiguos no se cambian automáticamente al arrancar. Ejecutar `scripts/migrate_beat_masters.py` en ventana de mantenimiento antes de invitar productores.

Los perfiles, búsqueda y listas follower/following no devuelven email a otros usuarios; `/v1/api/users/users/me` y la respuesta propia del registro conservan ese dato para el titular.

El watermark es obligatorio en producción. Con `BEAT_WATERMARK_ENABLED=true` y un archivo válido en `BEAT_WATERMARK_FILE`, FFmpeg lo mezcla periódicamente en el preview. Solo desarrollo puede continuar sin tag y emite un warning. El master nunca se modifica.

## MASTER PROTECTION

- `BEATS_ORIGINAL_DIR` debe quedar fuera de `MEDIA_ROOT`; settings valida que no haya solapamiento. El nombre final usa ID del productor + UUID generado por backend, y el filename recibido solo queda como metadata.
- Ningún response de post incluye `master_key`, ruta temporal, SHA-256 ni claves internas. `audio_url` y `preview_url` contienen exclusivamente la URL del preview.
- No existe endpoint ni location interna para descargar el master. El propietario tampoco recibe el archivo mediante APIs públicas en esta fase.
- La garantía para masters preexistentes requiere migración y bloqueo de keys antiguas en Nginx. Los archivos en backups permanecen sujetos a la política de backup configurada en el servidor.

## API CHANGES

- `POST /v1/api/posts/upload`: añade campo multipart requerido `rights_confirmed=true`; si falta/false devuelve `422`. `audio_url` mantiene el formato URL, pero su contenido ahora es un preview MP3 por defecto.
- `PUT /v1/api/posts/update/{post_id}`: al reemplazar `audio_file`, exige `rights_confirmed=true`; solo metadata/portada conserva el comportamiento actual.
- `POST /api/v1/beat-analysis`: rate limit nuevo, contrato de respuesta de análisis igual.
- `POST /api/v1/beats/from-analysis`: JSON debe incluir `rights_confirmed: true`; genera preview y master privado.
- `GET /v1/api/posts/{post_id}/stream`: público; redirige únicamente a un beat `ready` y a su preview. `audio_url` continúa siendo la vía compatible.
- `POST /v1/api/copyright/reports`: nuevo, anónimo con `beat_id`, `reporter_email`, `reason`, `details`; limita solicitudes por IP y guarda estado `open`.
- Búsqueda de usuarios, perfiles y listas followers/following: email privado; el endpoint autenticado “me” conserva email.
- Login, refresh, logout, feed, detalle, búsqueda, profile, avatar, eliminación y contratos existentes no cambian intencionalmente.

El texto final de confirmación de derechos y cualquier texto de copyright/privacidad quedan **pendientes de revisión jurídica**. El backend registra versión y fecha, pero esta revisión no redacta términos legales.

## DATABASE / MODEL CHANGES

Mongo es schemaless. Posts nuevos guardan `master_key`, `preview_key`, `original_filename`, `original_sha256`, tamaños, duración, formatos/MIME, `processing_status`, `publication_status`, `rights_confirmed`, versión y fecha. `master_key` solo vive en DB y almacenamiento privado.

Se crea `CopyrightReports` con `_id`, reporter email opcional a nivel de modelo (requerido por endpoint), reporter user ID nullable, beat, reason/details, status `open`, timestamps, request ID e IP. Índices se crean al iniciar la API.

Eliminar un beat borra metadata, interacciones/letras asociadas, preview y master de almacenamiento primario. No se purgan backups; no se definió política de retención legal.

## NEW ENV VARIABLES

- `BEATS_ORIGINAL_DIR`: default fuera de `MEDIA_ROOT`; producción sugerida `/srv/beatnow/private/beats/originals`.
- `BEATS_PREVIEW_DIR`: default `MEDIA_ROOT/previews`; debe permanecer dentro de `MEDIA_ROOT`.
- `FFMPEG_BINARY`, `FFPROBE_BINARY`, `PREVIEW_DURATION_SECONDS=30`, `PREVIEW_FORMAT=mp3|m4a`, `PREVIEW_AUDIO_BITRATE=96k`.
- `BEAT_PROCESSING_TIMEOUT_SECONDS=120`.
- `BEAT_WATERMARK_ENABLED=true`, `BEAT_WATERMARK_FILE=<ruta privada>`, `BEAT_WATERMARK_INTERVAL_SECONDS=30` en producción.
- `RIGHTS_CONFIRMATION_VERSION=beta-1` (incrementar cuando cambie el texto confirmado).
- `BEAT_UPLOAD_RATE_LIMIT=10`, `BEAT_STREAM_RATE_LIMIT=120`, `COPYRIGHT_REPORT_RATE_LIMIT=5` por ventana configurada en `RATE_LIMIT_WINDOW_SECONDS`.
- `SEARCH_RATE_LIMIT=60` por ventana.
- `TRUSTED_HOSTS=api.beatnow.app,localhost,127.0.0.1`.

Confirmar que `/srv/beatnow/private/beats/originals` está fuera de cualquier `root`/`alias` público, no basta con que esté fuera de `MEDIA_ROOT`.

## FRONTEND MIGRATION CONTRACT

| Endpoint | Contrato anterior | Contrato revisado | Auth | Cambio frontend |
|---|---|---|---|---|
| `POST /v1/api/posts/upload` | Multipart cover/audio + metadata; response `audio_url` al audio subido | Mismo response legado; `audio_url` sirve preview generado. Añadir `rights_confirmed=true` | Sí | Añadir checkbox/confirmación antes de subir; texto final legal pendiente |
| `PUT /v1/api/posts/update/{id}` | Metadata y/o audio/cover opcionales | Igual; audio nuevo exige `rights_confirmed=true` | Sí, owner | Enviar campo al reemplazar audio |
| `GET /v1/api/posts/feed`, `GET /v1/api/posts/{id}`, `/v1/api/posts/user/{username}`, `/v1/api/users/posts/{username}`, búsqueda y saved posts | `audio_url`, `audio_key`, portada y metadata | URLs existentes ahora contienen preview; no se exponen claves/campos de original | Sí, como ahora | Player puede seguir usando `audio_url`; comprobar que no asume extensión del master |
| `GET /v1/api/posts/{id}/stream` | No existía | Redirect al preview público si está `ready` | No | Opcional; los clientes actuales pueden seguir con `audio_url` |
| `DELETE /v1/api/posts/{id}` | Elimina post propio | Igual; elimina además master privado | Sí, owner | Ninguno |
| `POST /api/v1/beat-analysis` | Sube y analiza audio | Igual con rate limit | Sí | Ninguno |
| `POST /api/v1/beats/from-analysis` | JSON análisis + metadata | Requiere `rights_confirmed: true`; guarda original privado y publica preview | Sí, owner del análisis | Añadir confirmación al publicar |
| `POST /v1/api/copyright/reports` | No existía | Alta de reporte, estado `open` | No; email obligatorio | Formulario futuro con beat, email, motivo y detalles |
| Auth (login/refresh/logout) | Access + refresh token | Igual; HS256, 15 min access, refresh rotatorio de 7 días | Según endpoint | Ninguno |

`audio_url` se conserva por compatibilidad y es alias de `preview_url`. El backend no devuelve claves de storage, SHA-256 ni rutas físicas. MP3 es el formato de preview por defecto aunque el master sea WAV/FLAC/M4A.

## SERVER CHANGES REQUIRED

No ejecutar estas instrucciones desde el repo. Validar primero los paths/usuarios reales del VPS.

### NGINX

- **CURRENT ASSUMPTION:** `res.beatnow.app` sirve `MEDIA_ROOT` (documentación actual: `/srv/beatnow/media`). No hay archivo Nginx en este repositorio.
- **REQUIRED CHANGE:** servir previews/portadas/avatar desde media; bloquear los paths viejos `/beats/{id}/audio.{wav,mp3,flac,m4a}` después de migrar. API reverse proxy con body limit y timeout compatibles con 50 MiB y 120 s de procesamiento; sobrescribir `X-Forwarded-For` con IP real validada. Mantener API escuchando solo en `127.0.0.1:8001`.
- **SUGGESTION:** añadir al vhost de `res.beatnow.app` una denegación antes del location de archivos:

  ```nginx
  location ~* ^/beats/[^/]+/audio\.(wav|mp3|flac|m4a)$ { return 404; }
  ```

  Para API, ajustar los valores a los actuales del servidor:

  ```nginx
  client_max_body_size 55m;
  proxy_connect_timeout 10s;
  proxy_send_timeout 150s;
  proxy_read_timeout 150s;
  proxy_set_header X-Forwarded-For $remote_addr;
  proxy_set_header X-Request-ID $request_id;
  ```

- **VERIFY:** `nginx -t`; verificar que toda URL vieja de master devuelve 404 y el preview abre con `206/200`. Inspeccionar el vhost real y `root`/`alias` para confirmar que ningún root alcanza el directorio privado.
- **ROLLBACK/RISK:** guardar copia del vhost; un rollback de Nginx puede reabrir masters antiguos. No configurar ningún alias o location hacia masters.

### SYSTEMD

- **CURRENT ASSUMPTION:** servicio descrito como `beatnow.service`, usuario `beatnow`, working directory `/opt/beatnow-back`, bind loopback. No se verificó la unidad real.
- **REQUIRED CHANGE:** permitir escritura de `MEDIA_ROOT` y directorio original privado bajo `ProtectSystem=strict`; asegurar PATH a `ffmpeg`/`ffprobe`; limitar el acceso al puerto Uvicorn a loopback.
- **SUGGESTION:** adaptar la unidad existente, preservando otras opciones:

  ```ini
  [Service]
  User=beatnow
  Group=beatnow
  WorkingDirectory=/opt/beatnow-back
  EnvironmentFile=/opt/beatnow-back/.env
  ExecStart=/opt/beatnow-back/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8001
  NoNewPrivileges=true
  PrivateTmp=true
  ProtectSystem=strict
  ProtectHome=true
  ReadWritePaths=/srv/beatnow/media /srv/beatnow/private/beats/originals
  UMask=0027
  ```

- **VERIFY:** `systemd-analyze verify /etc/systemd/system/beatnow.service`; reiniciar en mantenimiento; `systemctl status beatnow`; `curl http://127.0.0.1:8001/readyz`; como usuario `beatnow`, `command -v ffmpeg; command -v ffprobe` y permiso de escritura en ambos directorios.
- **ROLLBACK/RISK:** conservar copia anterior; servicio no arrancará si el path o env no coincide con el servidor.

### LINUX PERMISSIONS

- **CURRENT ASSUMPTION:** no comprobable desde este repo.
- **REQUIRED CHANGE:** directorio master dedicado modo `0700`, archivos nuevos no más abiertos que `0600`; almacenamiento público legible por worker Nginx y escribible por `beatnow`.
- **SUGGESTION (ajustar grupo de Nginx real):**

  ```bash
  sudo install -d -o beatnow -g beatnow -m 0700 /srv/beatnow/private/beats/originals
  sudo install -d -o beatnow -g www-data -m 2770 /srv/beatnow/media/previews
  ```

  El grupo `www-data` es placeholder común en Ubuntu; confirmar `nginx -T`/proceso worker antes. No dar lectura del directorio privado a Nginx.
- **VERIFY:** `namei -l /srv/beatnow/private/beats/originals`; `sudo -u beatnow test -w ...`; el usuario Nginx no debe poder listar ni leer masters.
- **ROLLBACK/RISK:** corregir ownership/grupo en lugar de relajar permisos del directorio privado.

### DIRECTORIES

- **CURRENT ASSUMPTION:** `MEDIA_ROOT=/srv/beatnow/media` documentado; no se comprobó filesystem/volumen.
- **REQUIRED CHANGE:** crear directorio privado, confirmar que no está bajo raíz pública, y reservar espacio para master más preview. `BEATS_PREVIEW_DIR` tiene que estar dentro de `MEDIA_ROOT`.
- **SUGGESTION:** crear los directorios de arriba y configurar `BEATS_ORIGINAL_DIR=/srv/beatnow/private/beats/originals`, `BEATS_PREVIEW_DIR=/srv/beatnow/media/previews`.
- **VERIFY:** `realpath` de ambos, `df -h`, prueba upload y comprobación de que solo aparece `preview.*` bajo media pública.
- **ROLLBACK/RISK:** no borrar masters migrados durante rollback; preservar DB y ambos volúmenes.

### FIREWALL

- **CURRENT ASSUMPTION:** UFW/security group no está en repo.
- **REQUIRED CHANGE:** entrada pública solo SSH administrado, HTTP y HTTPS; bloquear acceso externo a 8001 y Mongo. Mongo Atlas debe aceptar solo egreso/IP autorizadas.
- **SUGGESTION:** inspeccionar `sudo ufw status verbose` y reglas del proveedor antes de cambiar; política objetivo típica: permitir SSH/80/443 y denegar 8001. No ejecutar reglas ciegas que puedan cortar SSH.
- **VERIFY:** escaneo desde fuera de la red confirma solo puertos esperados; `ss -ltnp` confirma Uvicorn en loopback.
- **ROLLBACK/RISK:** mantener sesión SSH abierta y regla de recuperación de SSH del proveedor antes de editar firewall.

### TLS

- **CURRENT ASSUMPTION:** dominios documentados usan Nginx/TLS; certificados no comprobados.
- **REQUIRED CHANGE:** TLS válido para `api.beatnow.app`, `res.beatnow.app`, redirects HTTP→HTTPS; conservar HSTS solo cuando todos los subdominios cubiertos sean HTTPS.
- **SUGGESTION:** revisar `sudo certbot certificates` y configuración TLS del vhost; no renovar ni cambiar issuer desde esta revisión.
- **VERIFY:** `curl -I https://api.beatnow.app/healthz`, revisar cadena y expiración con `openssl s_client`.
- **ROLLBACK/RISK:** cambio de TLS puede dejar endpoints inaccesibles; conservar vhost/cert vigente.

### BACKUPS

- **CURRENT ASSUMPTION:** roadmap recomienda backups diarios de media y Atlas, pero no hay prueba aquí de que existan/restauren.
- **REQUIRED CHANGE:** incluir `MEDIA_ROOT` y `BEATS_ORIGINAL_DIR` en backup persistente con control de acceso; Mongo backup debe incluir `Posts` y `CopyrightReports`. Acordar retención con asesoría/política del producto; no fijada en este documento.
- **SUGGESTION:** antes de migrar, backup consistente de Mongo y `MEDIA_ROOT`; verificar destino cifrado y separado del VPS conforme a la política vigente.
- **VERIFY:** restauración de muestra de ambos árboles y Mongo en entorno aislado; hashes de master restaurados coinciden con `original_sha256`.
- **ROLLBACK/RISK:** borrar un post en producción no borra copias históricas de backups.

### ENVIRONMENT

- **CURRENT ASSUMPTION:** `.env` de producción no es visible aquí.
- **REQUIRED CHANGE:** revisar `.env` del VPS sin copiarlo al repositorio; configurar rutas, límites, tag, bitrate, origins/hosts reales, `TRUSTED_PROXY_IPS`, strong secret y `PROMETHEUS_ENABLED=false` salvo acceso restringido.
- **SUGGESTION:** comparar nombres con `.env.example`; `BEATS_ORIGINAL_DIR=/srv/beatnow/private/beats/originals`, `BEATS_PREVIEW_DIR=/srv/beatnow/media/previews`, `CORS_ORIGINS=https://app.beatnow.app,https://beatnow.app,https://www.beatnow.app`.
- **VERIFY:** startup sin errores, `/readyz`, prueba de upload; verificar permisos y outputs sin imprimir secretos.
- **ROLLBACK/RISK:** backup protegido del env anterior; no rotar `SECRET_KEY` como parte de esta actualización porque invalida JWT existentes.

### LOG ROTATION

- **CURRENT ASSUMPTION:** app emite JSON a stdout; systemd/journald retiene los logs. Retención real desconocida.
- **REQUIRED CHANGE:** configurar persistencia/rotación/capacidad de journald o collector existente para evitar disco lleno; restringir acceso porque los eventos incluyen IP y user ID.
- **SUGGESTION:** revisar `journalctl --disk-usage`, `/etc/systemd/journald.conf` y política actual antes de modificar.
- **VERIFY:** generar login fallido/upload de prueba y localizar por request ID; confirmar que no aparecen headers/tokens ni cuerpos.
- **ROLLBACK/RISK:** retención demasiado baja elimina evidencia útil; demasiado alta consume disco.

### FAIL2BAN

- **CURRENT ASSUMPTION:** estado desconocido; app tiene límite local en memoria.
- **REQUIRED CHANGE:** opcional para beta; si se habilita, consumir access/error log correcto de Nginx y evitar bloquear por falsos positivos detrás de proxy. No sustituye rate limit distribuido.
- **SUGGESTION:** inspeccionar `fail2ban-client status`; adaptar jail existente a formato de logs real antes de crear uno.
- **VERIFY:** validar regex con muestras y probar un bloqueo de IP controlado.
- **ROLLBACK/RISK:** regex/proxy mal configurados pueden banear el proxy o productores legítimos.

### DATABASE EXPOSURE

- **CURRENT ASSUMPTION:** MongoDB se configura por env/Atlas; red y usuarios no visibles aquí.
- **REQUIRED CHANGE:** TLS, usuario de mínimo privilegio para DB BeatNow, acceso de red limitado al origen del VPS, backups probados; verificar que DB no escucha públicamente.
- **SUGGESTION:** revisar reglas Network Access, Database Access y auditoría del proveedor sin revelar URI/contraseñas.
- **VERIFY:** desde VPS `/readyz`; desde host externo no autorizado la conexión falla; índices aparecen para reports y SHA-256.
- **ROLLBACK/RISK:** cambiar allowlist sin IP de salida correcta puede dejar API fuera de DB.

### STORAGE

- **CURRENT ASSUMPTION:** `LocalStorageProvider`, `MEDIA_ROOT` en VPS, dominio de media dedicado. Nginx/CDN puede cachear archivos conforme a config externa.
- **REQUIRED CHANGE:** ejecutar migración, purgar cachés/CDN para keys antiguos, bloquear acceso a masters legacy, asegurar directorio master fuera de media/public roots y revisar snapshots.
- **SUGGESTION / SECUENCIA:**

  ```bash
  # 1. En mantenimiento, detener o pausar uploads y respaldar DB + ambos árboles.
  cd /opt/beatnow-back
  ./.venv/bin/python scripts/migrate_beat_masters.py
  ./.venv/bin/python scripts/migrate_beat_masters.py --execute
  # 2. Repetir dry-run: cada beat reproducible debería indicar would_verify.
  ./.venv/bin/python scripts/migrate_beat_masters.py
  # 3. Aplicar denegación Nginx de audio legacy, probar y purgar cache/CDN de keys antiguas.
  ```

  Ejecutar primero con `--post-id <OBJECT_ID>` sobre un beat de prueba. Revisar el hash metadata y comparar tamaño/master antes de borrar el audio público legacy. No iniciar beta hasta que ningún master siga bajo `MEDIA_ROOT` ni una URL antigua lo entregue.
- **VERIFY:** `find /srv/beatnow/media/previews -type f` inspeccionado; rutas antiguas devuelven 404; el preview funciona; no existe ruta de original; `sha256sum` coincide con DB.
- **ROLLBACK/RISK:** el script es dry-run por defecto, pero `--execute` reemplaza la URL/key de audio y elimina el archivo antiguo tras confirmar DB. Mantener backup; rollback de backend sin restaurar config/media podría publicar de nuevo la referencia antigua.

## TESTS RUN

- `./.venv/bin/python -m compileall -q main.py config core model routes services scripts` — pasó.
- `./.venv/bin/python -m pip check` — no dependency conflicts.
- `./.venv/bin/python -m pytest -q` — 80 passed.
- Las pruebas de FFmpeg simulan `ffprobe`/`ffmpeg`; todavía hace falta una prueba de integración en el VPS con los binarios reales y un beat de prueba.

## KNOWN LIMITATIONS

- Ningún servicio de streaming puede impedir al 100% que alguien capture o grabe el audio que escucha. El preview reduce exposición del master, no evita grabación.
- Preview público puede compartirse/capturarse y se sirve en el host de media; no hay DRM ni compras/licencias.
- Watermark depende de un archivo provisto por BeatNow; no se crea una voz/sonido automáticamente.
- Los masters existentes solo dejan de estar expuestos después de completar migración, restricción Nginx y purge de caché.
- Copyright report tiene intake y estados, sin panel interno/flujo de moderación/notificaciones.
- Rate limiting es por proceso y memoria; usar política de Nginx/proveedor o almacenamiento compartido antes de escalar workers/instancias.
- El texto de rights confirmation, términos, política de privacidad, procedimiento de reclamación y retención requieren revisión legal; no están definidos aquí.
- La suite local no valida permisos, Nginx, TLS, FFmpeg instalado en producción, CDN cache ni Atlas firewall.

## TODO BEFORE PUBLIC BETA

1. Actualizar web/mobile para incluir la confirmación en upload y publicar desde BeatAnalyser; probar edición de audio y descargar master solo desde panel autenticado.
2. Revisión jurídica del texto de confirmación, términos, privacidad y proceso de copyright report.
3. Configurar y verificar VPS conforme a la sección anterior; comprobar FFmpeg/ffprobe reales y `/readyz`.
4. Ejecutar migración dry-run, respaldar, migrar todos los beats y verificar hashes/archivos.
5. Aplicar bloqueo Nginx de audio legacy, probar owner A/B y purgar caché de recursos antiguos.
6. Verificar backups/restauración, Mongo exposure, TLS, firewall y logging/rotación.
