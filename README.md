# JARVIS MVP

Phone (voice in/out + device actions)  --HTTP JSON-->  Laptop FastAPI server (AI brain + permissions + plans)

- `server/` – FastAPI. `providers.py` (AI_PROVIDER: `offline` | `anthropic` | `azure_openai`), `tools.py` (the only
  tools that can run), `permissions.py` (capability grants), `plans.py` (approval/confirmation engine), `offline.py`.
- `android/` – Kotlin + Compose + Material 3 app (MVVM: ViewModel + StateFlow, Navigation Compose), four tabs:
  - **Assistant** – animated arc-reactor mic, live transcript, chat, typed commands; speaks replies.
  - **Assistant** also shows plans: progress, Approve / Resume / Retry, and a confirmation dialog before anything is sent.
  - **App Access** – server URL + health check, JARVIS capability grants, device permissions, per-app allow/block.
  - **Tasks** – quick-execute console, success/failed/blocked stats, execution log with re-run.
  - **History** – persisted conversations grouped by day, search, delete/clear.
  Code: `data/` (client, repositories, action executor), `voice/VoiceEngine.kt`, `ui/<screen>/` (Screen + ViewModel).
  Default server URL is `http://127.0.0.1:8000` (USB + `adb reverse`); change it in App Access.

## Run
1. Server: edit `server\.env` (git-ignored; `.env.example` explains where each Azure value is found), set
   `AI_PROVIDER`, then run `server\run_server.bat` (listens on 0.0.0.0:8000).
   Config: `curl http://localhost:8000/provider/status` (never returns secrets).
   Connectivity: `curl -X POST http://localhost:8000/provider/check` (Azure: one tiny chat call; Anthropic: model lookup).
2. Allow port 8000 through Windows Firewall (admin PowerShell):
   `New-NetFirewallRule -DisplayName "JARVIS 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private`
3. Phone on the same Wi-Fi: install `android/app/build/outputs/apk/debug/app-debug.apk`
   (`adb install -r ...` or open in Android Studio and Run). Tap **Server** to set `http://<laptop-ip>:8000` (default 10.129.0.28).
4. Tap **TALK** and speak, or type a command.

### If the phone can't reach the laptop over Wi-Fi (campus networks often isolate clients)
Use USB instead — no firewall/Wi-Fi needed:
```
%LOCALAPPDATA%\Android\Sdk\platform-tools\adb reverse tcp:8000 tcp:8000
```
then set the app's Server URL to `http://127.0.0.1:8000`.

Build: `cd android && gradlew assembleDebug` (JAVA_HOME = Android Studio's `jbr`).
Tests: `cd server && python -m pytest tests`.

## Storage: MongoDB Atlas (optional)
Without configuration the server stores everything in `server/data/` (JSON files + `audit.jsonl`). That fallback:
- works for one server process on one machine only (two instances would overwrite each other),
- has no replication or backup (deleting `server/data/` loses tasks, grants and history),
- keeps the latest 5000 audit events, and can't be shared with another laptop.

To use Atlas instead:
1. cloud.mongodb.com > create a project and a cluster (the free M0 tier is enough).
2. **Security > Database Access**: add a database user with a password and the *Read and write to any database*
   role (or scope it to the `jarvis` database).
3. **Security > Network Access**: add this laptop's public IP (avoid `0.0.0.0/0`).
4. **Database > Connect > Drivers > Python**: copy the `mongodb+srv://...` string, insert the password
   (URL-encode characters like `@ : / ? #`), and put it in `server/.env`:
   ```
   MONGODB_URI=mongodb+srv://jarvis:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   MONGODB_DB_NAME=jarvis
   ```
5. `pip install -r server/requirements.txt` (adds `pymongo`), restart the server, then check
   `curl http://localhost:8000/storage/status` -> `"backend": "mongodb_atlas", "ok": true` (no credentials are shown).

Collections: `tasks` (plans with steps and checkpoints), `grants`, `approvals`, `idempotency`, `audit`.
If `MONGODB_URI` is set but Atlas can't be reached, `ok` is `false` and the server refuses plans, grants and
gated actions. It does **not** fall back to local files, so records never end up split across two stores.
Existing local data is not migrated to Atlas automatically.

## Integrations (all optional; each reports `NOT_CONFIGURED` until set up)
Check everything with `curl http://localhost:8000/integrations/status` or the app's **App Access › Integrations**.
Keys and tokens stay in `server/.env` / server storage: never in the APK, API responses, logs or Git.
Every operation still needs a capability grant (App Access › JARVIS capabilities).

### Phone: camera, microphone, voice (no setup)
- **Camera**: "take a photo" asks for the CAMERA permission the first time, opens the camera, and counts the
  step as done only if a non-empty photo was saved (Pictures/JARVIS). Cancel or denial = failed, never faked.
  Needs the `camera:capture` grant.
- **Microphone**: only while you tap the core (or an approved plan's `listen` step while JARVIS is on screen);
  listening stops when the app leaves the screen. Nothing is recorded in the background.
- **Voice**: Android text-to-speech. If no voice is installed the app says so and shows text only.

### Hands-free (background listening)
Assistant › Hands-free › **Start** (asks for microphone + notification permission). A microphone foreground
service keeps a persistent "JARVIS hands-free is on" notification with **Stop listening**; Android also shows its
green mic indicator. Only speech that starts with **"Jarvis …"** is sent to the server; everything else is dropped
on the phone. It uses the phone's own speech recognizer (here: Google Speech Services), which may process audio
online and plays its own start sounds. Limits, by design:
- It stops on Stop, if the mic permission is removed, after 6 consecutive recognizer errors, or if Android kills
  it; it never restarts itself. Xiaomi: set Battery saver to "No restrictions" and allow Autostart.
- Android blocks apps from opening other apps from the background, so while JARVIS isn't on screen actions
  (open app, timer, alarm…) become a "Tap to finish" notification instead of being reported as done; the camera
  needs JARVIS on screen. Questions are answered and spoken in the background.
- Tap-to-speak still works when hands-free is off.

### Microsoft 365: Outlook mail + calendar (Microsoft Graph)
> Sign-in stuck on CONNECTING? Device-code sign-in needs no redirect URI. Check the registration's *Supported
> account types*: a **single-tenant** app (only your tenant id works as `MS_GRAPH_TENANT`) rejects personal
> Microsoft accounts on Microsoft's page, and the code then simply expires. Sign in with an account from that
> tenant, or change the registration to include personal accounts and set `MS_GRAPH_TENANT=common`.
> App Access shows Microsoft's error code (AADSTS…) when sign-in fails.
1. entra.microsoft.com > **App registrations > New registration**. Name "JARVIS". Supported accounts:
   *Accounts in any organizational directory and personal Microsoft accounts* (or narrower). No redirect URI.
2. In the app: **Authentication > Advanced settings > Allow public client flows = Yes** (device-code sign-in).
3. **API permissions > Add > Microsoft Graph > Delegated**: `User.Read`, `Mail.ReadWrite`, `Mail.Send`,
   `Calendars.ReadWrite`, `offline_access`. (Work accounts may need admin consent.)
4. Copy **Application (client) ID** to `MS_GRAPH_CLIENT_ID`; set `MS_GRAPH_TENANT` (`common`, `consumers`,
   `organizations` or your tenant id), `JARVIS_TIMEZONE` and a long random `JARVIS_SECRET_KEY` (encrypts tokens).
5. Restart the server, then App Access › Integrations › **Connect Microsoft account**: the app shows a code and
   opens microsoft.com/devicelogin. Or `curl -X POST localhost:8000/integrations/graph/connect`.

What it does (grants in brackets):
- "summarize my email" [email:read] — Outlook inbox via Graph; summary by the AI provider, or a labelled rule-based
  summary in offline mode. Only sender/subject/preview are used; email bodies are not stored or audited.
- "draft an email to …" [email:draft] — plan approval, saved in Drafts and read back.
- "email bob@x.com about … saying …" [email:send] — plan approval **and** final confirmation. Sent as
  draft-then-send with an immutable id, confirmed when it leaves Drafts; a retry checks the saved draft first, so
  it can't send twice. A timeout after sending becomes "uncertain" and waits for you.
- "what's on my calendar tomorrow" [calendar:read] — events listed as E1, E2, …
- "add event Dentist on 2026-10-21 10:00" [calendar:write] — approval + confirmation, created with a Graph
  `transactionId` (no duplicates on retry) and verified by reading the event back.
- "move E2 to 2026-10-21 11:00" / "rename E2 to …" [calendar:write] — same rules, verified by read-back.
Without Graph, email/calendar use the phone hand-offs from earlier modules (user finishes in the other app).

### WhatsApp
- **Default (no setup)**: JARVIS opens WhatsApp with the message pre-filled, or the Android share sheet
  ("share …"); you press send. JARVIS then asks whether it was sent. It has no access to your personal chats.
- **WhatsApp Business Platform** (only with an authorized Meta setup): developers.facebook.com > create an app
  (type *Business*) > add **WhatsApp** > in *API Setup* note the **Phone number ID**; in Meta Business Settings
  create a **System user**, give it the app and WhatsApp account, and generate a permanent token with
  `whatsapp_business_messaging`. Put them in `WHATSAPP_ACCESS_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID`.
  Limits you will hit: messages come from the *business* number; free-form text only reaches people who messaged
  that number in the last 24 h (otherwise Meta requires approved templates, not implemented); "accepted" is
  reported, delivery is not (no webhook). Needs whatsapp:send and a confirmed plan; timeouts are never resent.

### Azure AI Speech (optional neural voice)
1. portal.azure.com > **Create a resource > Speech** (Azure AI services). Pick a region near you (e.g.
   *Central India*) and the Free F0 or Standard S0 tier.
2. Resource > **Keys and Endpoint**: copy **KEY 1** to `AZURE_SPEECH_KEY` and **Location/Region** (e.g.
   `centralindia`) to `AZURE_SPEECH_REGION`. Optionally `AZURE_SPEECH_VOICE` (default `en-GB-RyanNeural`).
3. Restart; App Access › Integrations › Azure Speech shows CONFIGURED; switch on "Speak replies with the Azure
   voice" and grant `speaker:speak`. If Azure fails, the app says so and uses the phone's voice.
Server endpoints: `POST /speech/tts` (speaker:speak) returns MP3; `POST /speech/stt` (microphone:listen) takes a
16 kHz mono WAV clip (≤ 60 s) and returns text. The Android app does not yet record audio for `/speech/stt`;
it keeps using Android's on-device recognizer.

## Reliability model
- **Task states**: `awaiting_approval → running → (awaiting_confirmation) → awaiting_result → …`, plus `blocked`,
  `retry_scheduled`, `awaiting_verification`, `step_failed`, `completed`, `cancelled`. Every step keeps timestamped
  checkpoints and an attempt counter; `GET /tasks` and the app's **Tasks › Plan history** show them.
- **Commit order**: each transition writes its audit events, then saves the task, and only then takes effect. If
  storage fails nothing changes and nothing is dispatched (HTTP 503).
- **Before every dispatch** (first run, resume, automatic or manual retry, re-dispatch after restart): plan approval
  (valid 24 h), capability grant, and, for sends/calendar changes, a fresh confirmation for that exact attempt.
- **Errors**: `transient` (the phone knows nothing happened) → automatic retry after 2 s, 4 s … (cap 60 s, max 3
  attempts). `permanent` → step fails, manual retry possible. `uncertain` → never retried until the user verifies.
  AI-provider errors are labelled transient/permanent; the SDKs already retry transient ones with backoff.
- **Verified outcomes**: a step is `succeeded` only with evidence (e.g. activity launched). Emails, WhatsApp
  messages and calendar events open in another app, so they become `handed_off` until the user answers
  "Did this happen?".
- **No duplicates**: every dispatch has a `dispatch_id`; the server applies one result per id, and the phone keeps a
  ledger so a repeated id is answered without running the action again. After a server restart, in-flight
  open/search/camera steps are re-dispatched with the same id; alarms, timers, sends and calendar events become
  `uncertain` instead. Mutating endpoints honour an `Idempotency-Key` header (replays return the first response).
- **Audit log** (`GET /tasks/{id}/audit`): event type, step, attempt, timestamps. Message text is replaced by its
  length, recipients are masked (`**********78`), anything resembling a key or connection string is redacted.

## Safety model
- **Providers**: chosen only by `AI_PROVIDER`; a misconfigured provider returns an error, it never falls back.
  Azure endpoint type decides the client: `https://<res>.openai.azure.com/` -> `AzureOpenAI` (needs api-version);
  `.../openai/v1/` -> `OpenAI(base_url=...)`; Foundry project (`/api/projects/`) and `/models` endpoints are rejected.
- **Tools**: the model can only name tools in `tools.py`; arguments are validated (types, formats, lengths, no extras).
  Unknown tools reject the whole request. No code or shell is ever executed.
- **Capabilities** (email, whatsapp, camera, microphone, speaker, calendar): default-deny. Grants are per scope
  (e.g. calendar read vs write), optionally time-limited and recipient-limited, revocable, stored in `server/data/`.
  Checked before every dispatch: first run, resume, retry, and phone-side re-runs (`/actions/authorize`).
- **Plans**: multi-step or consequential requests (send email/WhatsApp, add calendar event) become a plan the user
  approves; each consequential step also needs an explicit final confirmation, reset on retry.
  Email/WhatsApp/calendar open the phone's own app pre-filled; the user's last tap in that app actually sends.
- Mic/speaker grants govern JARVIS-initiated `listen`/`speak` tools; your own push-to-talk and spoken replies are unchanged.
- The server has no client authentication yet: anyone who can reach port 8000 can call it (including granting).
  Keep it on USB/`adb reverse` or a trusted network.
