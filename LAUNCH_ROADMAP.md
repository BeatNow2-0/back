# BeatNow Launch Roadmap

## Objetivo

Lanzar BeatNow como producto usable con:

- API estable para auth, catálogo, feed, perfiles y lyrics.
- `app-web` para productores con subida y gestión de beats.
- `web` como landing pública.
- `movil-app` para discovery, búsqueda y guardado de beats.

## Arquitectura recomendada de lanzamiento

### Infra mínima

- `MongoDB Atlas` para base de datos.
- `1 VPS Ubuntu` para API FastAPI + Nginx + systemd.
- `1 bucket/object storage + CDN` para media.
  Actualmente la app asume `https://res.beatnow.app/beatnow`.
- `1 hosting estático` para `web` y `app-web`.
  Recomendado: Vercel o Netlify.
- `SMTP transaccional` para confirmación y reset de contraseña.
  Recomendado: Postmark, Resend o SES.

### Dominios

- `beatnow.app` y `www.beatnow.app`: landing pública `web`
- `app.beatnow.app`: `app-web`
- `api.beatnow.app`: backend FastAPI
- `res.beatnow.app`: media/CDN

## Fase 1: Pre-lanzamiento técnico

### Backend

- Congelar contrato API de auth, posts, follows, interactions y lyrics.
- Cargar índices Mongo en producción y validar tiempos de respuesta.
- Configurar backups automáticos de MongoDB Atlas.
- Mover secretos a variables de entorno reales.
- Verificar CORS para `beatnow.app`, `www.beatnow.app` y `app.beatnow.app`.

### Web productor

- Probar login, registro, verificación email, subida de beat, edición y borrado.
- Añadir gestión real de refresh token si el panel va a tener sesiones largas.
- Revisar rutas rotas y UX de expiración de sesión.

### Móvil

- Test manual completo en iOS y Android:
  login, feed, like/save, búsqueda, perfiles, lyrics, reset password.
- Configurar iconos definitivos, splash, nombre final y bundle IDs.
- Revisar audio playback y comportamiento al background.

## Fase 2: Beta cerrada

### Objetivo

Validar producto con 10-30 productores y 30-100 artistas.

### Qué medir

- ratio de registro a activación de cuenta
- ratio de subida de primer beat
- número de beats guardados por usuario
- retención D1 y D7 en móvil
- errores 4xx/5xx por endpoint

### Acciones

- Instrumentar logs estructurados en API.
- Activar métricas Prometheus si el VPS lo permite.
- Crear tablero mínimo con:
  requests/min, p95 latency, 5xx rate, login failures, reset failures.
- Abrir canal de feedback con usuarios beta.

## Fase 3: Release Candidate

### Checklist

- Congelar features nuevas 7-10 días.
- Corregir bugs críticos de auth, subida de media y búsqueda.
- Auditar textos legales:
  términos, privacidad, copyright/DMCA, soporte.
- Preparar contenido App Store / Play Store:
  screenshots, description, privacy answers.

### Go/No-Go

- API p95 < 400 ms en endpoints críticos sin subida de media.
- tasa de error 5xx < 1%.
- login success rate > 95%.
- subida de beat exitosa > 95% en beta.

## Fase 4: Lanzamiento público

### Día 0

- desplegar backend en ventana controlada
- desplegar `web` y `app-web`
- publicar builds móviles aprobadas o TestFlight/Open Testing
- monitorizar durante 4-6 horas

### Primera semana

- soporte activo diario
- hotfixes rápidos de UX, emails y media
- revisar cohortes de activación y engagement

## Despliegue de servidores

### API FastAPI en VPS

1. Ubuntu 22.04 LTS
2. Usuario de servicio `beatnow`
3. Código en `/opt/beatnow-back`
4. Virtualenv `.venv`
5. `gunicorn` + `uvicorn.workers.UvicornWorker`
6. `systemd` para proceso
7. `nginx` como reverse proxy TLS

### Nginx

- TLS con Let's Encrypt
- `client_max_body_size` suficiente para audio/covers
- rate limiting para `/v1/api/users/login` y `/v1/api/mail/*`
- cabeceras de seguridad

### Media

Idealmente mover media a object storage y servir por CDN. Si no:

- montar volumen persistente en `/var/lib/beatnow/media`
- backups diarios
- vigilancia de espacio en disco

### CI/CD

- `back/.github/workflows/ci.yml`: tests
- `back/.github/workflows/deploy_vps.yml`: despliegue backend por SSH
- `web` y `app-web`: despliegue automático en Vercel/Netlify por branch

## Prioridad inmediata

1. Lanzar backend estable en VPS con TLS y backups.
2. Lanzar `app-web` y `web` con dominios separados.
3. Cerrar beta móvil con analítica básica.
4. Publicar mobile cuando auth, búsqueda y feed estén estables.
