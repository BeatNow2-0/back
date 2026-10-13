# BeatNow fase 2 — protección de preview

## Estado implementado

- Cada beat nuevo persiste `master_key` y `preview_key`. El master se guarda bajo `BEATS_ORIGINAL_DIR`, que la configuración obliga a situar fuera de `MEDIA_ROOT`.
- `preview_url` es el contrato explícito y `audio_url` es un alias temporal idéntico. La serialización pública no usa `audio_key` legacy ni expone `master_key`, hash, nombre, ruta o error interno.
- Solo un beat con `processing_status=ready` y `preview_key` puede aparecer o reproducirse. Los estados persistidos son `processing`, `ready` y `failed`.
- FFprobe valida tipo, duración real y bitrate cuando está disponible. FFmpeg genera MP3/M4A independiente, limitado a 30 segundos por defecto (configurable), con tope de 96 kbps y reducción dinámica al 75% si el origen ya tiene un bitrate inferior. `preview_start` y `preview_end` se validan contra la duración real.
- Producción no arranca sin tag habilitado y un `BEAT_WATERMARK_FILE` legible. Desarrollo puede generar sin tag y deja un warning explícito.
- Cualquier usuario activo puede subir o reemplazar audio. No existe rol obligatorio ni endpoint de descarga del master.
- El procesamiento FFmpeg se desplaza a un threadpool para no bloquear el event loop, pero la primera versión sigue esperando el resultado dentro de la petición. No existe cola/worker en la infraestructura actual; antes de aumentar duración/tamaño o escalar horizontalmente debe añadirse una cola durable.

## Migración

Hacer backup de MongoDB y de ambos árboles de media. Ejecutar primero:

```bash
. .venv/bin/activate
python scripts/migrate_beat_masters.py --manifest /srv/beatnow/migrations/preview-phase2.jsonl
```

Probar un beat y después ejecutar el lote:

```bash
python scripts/migrate_beat_masters.py --post-id <OBJECT_ID> --execute --manifest /srv/beatnow/migrations/preview-phase2.jsonl
python scripts/migrate_beat_masters.py --execute --manifest /srv/beatnow/migrations/preview-phase2.jsonl
```

La migración es reejecutable. También reubica portadas legacy desde `beats/` hacia `covers/` verificando sus bytes. Antes de retirar un audio público comprueba SHA-256 del master privado, existencia del preview y decodificación mediante FFprobe. Un origen ausente, formato no soportado o verificación fallida deja el beat en `failed`, sin `preview_key`; nunca se fabrica una URL hacia el audio legacy.

Comprobaciones posteriores:

```bash
python scripts/migrate_beat_masters.py
find /srv/beatnow/private/beats/originals -type f -print
find /srv/beatnow/media/previews -type f -print
curl -I https://res.beatnow.app/previews/<BEAT_ID>/preview.mp3
curl -I https://res.beatnow.app/beats/<BEAT_ID>/audio.wav
```

El primer `curl` debe devolver 200/206 y el segundo 404. Revisar en MongoDB que todos los reproducibles tengan `processing_status: "ready"`, `master_key`, `preview_key`, `original_sha256` y que `audio_key == preview_key`.

## Rollback

No borrar el backup ni el manifiesto JSONL. Para rollback de aplicación, desplegar la versión anterior manteniendo bloqueado `/beats/`; no volver a exponer los masters. Para rollback de datos, restaurar MongoDB desde el backup o aplicar los valores `before` del manifiesto, y copiar desde el master privado verificado solo a un área de cuarentena no pública. Los masters privados permanecen disponibles y el script no los elimina. No habilitar reproducción hasta regenerar y verificar un preview.

## Nginx/VPS manual

1. Instalar `ffmpeg` y `ffprobe` y configurar las variables de `.env.example`.
2. Dar escritura al usuario de la API sobre `/srv/beatnow/private/beats/originals`, con directorios `0700`; Nginx no debe tener acceso a ese árbol.
3. Servir desde `MEDIA_ROOT` únicamente `/avatars/`, `/covers/` y `/previews/`. Denegar `/beats/`, `/private/`, `/temp/`, dotfiles y cualquier alias al directorio privado.
4. Confirmar soporte `Range`, MIME `audio/mpeg`/`audio/mp4`, ejecutar `nginx -t`, recargar y purgar cachés/CDN de rutas legacy.

Configuración orientativa (adaptar al vhost real):

```nginx
location ~ ^/(avatars|covers|previews)/ { try_files $uri =404; }
location ~ ^/(beats|private|temp)/ { return 404; }
location ~ /\. { return 404; }
```

## Verificación local

```bash
.venv/bin/python -m compileall -q config core model routes services scripts tests
.venv/bin/python -m pytest -q
```

Resultado de esta entrega: `87 passed`, `compileall` correcto, import/build de la aplicación correcto, `pip check` sin conflictos, `git diff --check` limpio y contrato OpenAPI con `preview_url`/`audio_url` sin endpoint de master. El entorno local no tiene `ffmpeg`, `ffprobe` ni un linter instalados; las pruebas del pipeline simulan ambos binarios y queda pendiente el smoke test real indicado para VPS. La configuración real de Nginx/VPS y la migración de producción son acciones manuales; este repositorio no puede validarlas por sí solo.
