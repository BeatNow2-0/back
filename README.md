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

## Beat Analyzer MVP

`POST /api/v1/beat-analysis` accepts a multipart `file` (WAV, MP3, FLAC, M4A), stores it under the private sibling directory `MEDIA_ROOT/../.beat-analysis-tmp/{uuid}`, and returns an analysis result. `GET` and `DELETE /api/v1/beat-analysis/{analysis_id}` are owner-only. Publish without uploading again with `POST /api/v1/beats/from-analysis` and JSON `{ "analysis_id": "…", "title": "…" }` plus the normal post fields. The original file is moved through the existing beat storage staging pipeline. Unpublished files expire after `BEAT_ANALYSIS_TTL_HOURS` (default 24); run `./.venv/bin/python scripts/cleanup_beat_analyses.py` periodically from cron or a scheduled job. The async function is also available as `routes.beat_analysis_routes.cleanup_expired_beat_analyses`.

Set `BEAT_ANALYSIS_MAX_UPLOAD_SIZE` (default 100 MiB), `BEAT_ANALYSIS_MAX_DURATION_SECONDS` (default 600), and `BEAT_ANALYSIS_TTL_HOURS`. FFmpeg and FFprobe must be installed on the host. Analysis uses a normalized 44.1 kHz stereo PCM WAV. The current analyzer measures duration, sample rate/channels, RMS-derived energy, sample peak, clipped sample ratio, 10th-to-95th percentile one-second window dynamics, channel correlation, mid/side stereo width, and a coarse loudest-window preview. BPM, key, LUFS, true peak and frequency-band balance remain null because no DSP dependencies are present in the existing environment and unvalidated substitutes would be misleading. Preview confidence remains null. Treat measured signal summaries and the preview heuristic as estimates, not mastering or musical truth. A worker can replace the synchronous `analyze_beat` call while retaining the persisted status/result contract.

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
ReadWritePaths=/srv/beatnow/media /srv/beatnow/.beat-analysis-tmp
UMask=0027

[Install]
WantedBy=multi-user.target
```

Nginx should terminate TLS, proxy to `127.0.0.1:8001`, enforce request size limits and rate limiting on auth routes.
