# Speech API — local default and optional Personal Agent

All routes use the same-origin security middleware and existing authenticated sessions.
Cookie mutations require `X-Room-Request: 1`. Display credentials may upload audio and
access only jobs owned by that paired device; they never become admin credentials.
Admin can access jobs from all devices. Ingest-only tokens cannot use display routes.

| Route | Permission | Input / result |
|---|---|---|
| GET `/api/speech/status` | Viewer | ready/reason, effective backend/model, agent_enabled, length/size/capacity, queued/running |
| GET `/api/speech/jobs` | Viewer | own last 10 (admin20) jobs; no file paths |
| POST `/api/speech/jobs` | Viewer | multipart `file`, `request_id`; 202 job+duplicate |
| GET `/api/speech/jobs/{id}` | Owner/admin | status, transcript, safe error, duration/elapsed, stt_backend and safe agent_run |
| POST `/api/speech/jobs/{id}/cancel` | Owner/admin | idempotent cancellation |
| POST `/api/speech/jobs/{id}/retry` | Owner/admin | same record, max3 total attempts |
| POST `/api/speech/enqueue/{voice_id}` | Admin | manually queue a legacy uploaded audio inbox entry |
| GET `/roomhub-ca.cer` | Public | ONLY generated public root certificate, never key |

States: queued → running → succeeded / failed / cancelled.
A successful job puts its linked voice entry into `pending_review`. The optional
agent connection described below may execute reads/timers or stage other changes.
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

The default local path uses no Web Speech/Siri API or cloud inference. LAN HTTPS remains necessary
for browser microphone access. Public model/build downloads occur only at setup; the
existing weather feature still uses its weather provider.

## Optional Personal Agent integration

The original local/remote Whisper configuration remains separate. OpenAI STT requires
all three saved policy gates (`agent_enabled`, `cloud_enabled`, `audio_cloud_enabled`),
a dedicated server key, available budget and local FFmpeg validation for browser audio.
Policies are seeded from environment only when first created; subsequent changes are
saved through the manager UI/API. `/api/speech/status` reports the effective backend
(`openai` or the configured Whisper backend) without sending audio or probing OpenAI.
Readiness is local prerequisite/worker readiness, not verified provider availability.

The internal transcription callback derives authority from the persisted job owner and
rechecks pairing. A display cannot supply an admin identity, read another device's run,
or call the general admin-only `/api/agent/command`, `/audio`, or confirmation routes.

`agent_run` is null without an agent link, a pending/failed/interrupted status during
handoff, or an owner-checked safe run view once linked. Private and stale memo-derived
content is redacted on each read. A display never receives the raw plan/preview/receipt.
The existing UI continues polling after STT success while the agent is pending, running,
or awaiting manager confirmation. Closing/reopening or network recovery uses GET on
the same job; it never automatically submits audio, calls a model, or approves a change.

Audio ingress and provider reservation keys persist across retries and restart. A manual
retry of a failed cloud attempt does not authorize a second paid dispatch with the same
key. Uncertain outcomes retain reserved cost rather than claiming zero spend. General
changes still require manager confirmation; only validated reads and bounded timers
may execute immediately. See [Personal Agent](OPENAI_PERSONAL_AGENT.md).
