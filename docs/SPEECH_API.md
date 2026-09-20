# Local speech API — 0.1.3

All routes use the same-origin security middleware and existing authenticated sessions.
Cookie mutations require `X-Room-Request: 1`. Display credentials may upload audio and
access only jobs owned by that paired device; they never become admin credentials.
Admin can access jobs from all devices. Ingest-only tokens cannot use display routes.

| Route | Permission | Input / result |
|---|---|---|
| GET `/api/speech/status` | Viewer | ready/reason, model, threads, length/size/capacity, queued/running |
| GET `/api/speech/jobs` | Viewer | own last 10 (admin20) jobs; no file paths |
| POST `/api/speech/jobs` | Viewer | multipart `file`, `request_id`; 202 job+duplicate |
| GET `/api/speech/jobs/{id}` | Owner/admin | status, transcript, safe error, duration and elapsed |
| POST `/api/speech/jobs/{id}/cancel` | Owner/admin | idempotent cancellation |
| POST `/api/speech/jobs/{id}/retry` | Owner/admin | same record, max3 total attempts |
| POST `/api/speech/enqueue/{voice_id}` | Admin | manually queue a legacy uploaded audio inbox entry |
| GET `/roomhub-ca.cer` | Public | ONLY generated public root certificate, never key |

States: queued → running → succeeded / failed / cancelled.
A successful job puts its linked voice entry into `pending_review`; it does not create a task.
No automatic retry on engine failure, and interrupted running work is marked failed at restart.
Queued jobs persist in SQLite and are processed when the configured engine worker starts.

Supported input containers: browser MP4/AAC or WebM/Opus, plus WAV/MP3/OGG/FLAC/AAC.
MIME and magic are checked; FFmpeg is the actual decoder and rejects malformed input.
Decoder network protocols and non-audio demuxers are restricted. Max default input8MiB,
30s, total active+queued4; 128MiB quota considers stored voice files. Quota is not a full
Android-disk accounting system and old unrestricted legacy intake remains admin-only.

Whisper worker is a child process, one at a time, `nice 10`, CPU2 threads. Temp decoded
WAV and output files are private and deleted after normal completion/error/cancellation.
Original upload is retained for review until admin deletion. A raw kill/power loss can
leave temporary files; stop the server before cleaning `data/speech-tmp`.

DB change: additive speech_jobs table + queue index only. Existing tasks/layout/sessions
and voice rows are not rewritten by the update. Deletes cascade from voice to speech_jobs.

Configuration: private `data/speech-config.json`. Never send arbitrary executable/model
paths from client requests. Readiness is local config/binary/model existence plus worker
availability, not a per-request benchmark; installation invokes a separate real self-test.

Speech config install/download is manual, never a request side effect. Keep one Uvicorn
worker as in the existing installation. Multiple independent backend instances cannot share
this single-consumer design without external queue coordination.

No Web Speech/Siri API or cloud inference request is used. LAN HTTPS remains necessary
for browser microphone access. Public model/build downloads occur only at setup; the
existing weather feature still uses its weather provider.
