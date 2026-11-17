# AgriSentinel

A complete local agricultural rover monitoring platform: Flask, SQLAlchemy, persistent SQLite, Jinja, vanilla JavaScript, bundled Chart.js, authenticated REST APIs and SSE. A **built-in demonstration feed** updates three virtual rovers every five seconds; no separate simulator is required. The optional legacy HTTP simulator remains available for protocol testing in device-only mode. Monitoring requires no physical hardware, build step or runtime CDN. The optional chatbot and voice mode use DeepInfra.

The interface uses graphite surfaces (`#0a0a0a` / `#121212`), high-contrast neutral text, IBM Plex Sans and IBM Plex Mono, blue actions and selection, labeled status indicators, readable tables and reusable CSS tokens. There are seven working sections: overview, field monitoring, rover management, telemetry analytics, alerts, reports and a farm chatbot.

## Run locally

Python **3.11+** is required. This checkout was tested on Windows with Python 3.14.5. From the repository directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Optional: configure credentials and simulator behavior before first startup.
Copy-Item .env.example .env
```

Terminal 1:

```powershell
.\.venv\Scripts\python.exe app.py
```

Open **http://127.0.0.1:5000**. On first startup the backend generates a local operator password. Read `instance/admin-credentials.txt` and sign in as `operator`. If `ADMIN_PASSWORD` is set in `.env`, use that password instead. Explicit passwords must contain at least 12 characters.

The dashboard starts with a labeled fictional rabi-season farm: wheat in zones A, B and E, chickpea in C and D, and a fallow plot in F. Shared day/night weather, inverse temperature/humidity changes, gradual soil drying and staggered two-day irrigation scenarios keep the values coherent. Rovers patrol and stop at checkpoints during the day and idle/charge overnight. Values represent an illustrative scenario, not local weather or physical measurements.

On first activation, the app generates 30 days of half-hourly demo history. Charts connect stored demo samples with bounded curves; real device histories retain missing-data gaps. Fresh readings are then persisted every five seconds through the built-in service. Startup backfills gaps at half-hour intervals; repeated startup preserves existing history. The original provisioned virtual rovers are adopted as demo devices, and missing demo rovers are added up to three. Together with the registered ESP32 camera rover, the local fleet has four rovers. Each demo rover patrols two plots during the day and parks overnight. Surplus rovers from the earlier six-demo configuration are archived under `instance/backups/` before removal. Physical devices and their keys are preserved. The crop plan and sowing dates are stored in the database; growth stages are calendar-based assumptions.

The chatbot receives the same stored readings and crop plan, current alerts and commands, project capabilities/limitations, and a past-24-hour statistical summary. It can answer judges' questions about crops, conditions, thresholds and implemented features without presenting generated data as tested hardware results. DeepInfra credentials are still required for model answers.

In Reports, Generate, both CSV actions and Print / PDF respect the selected period, zone and ending time. CSV uses the exact bounds of the generated on-screen report. Daily, weekly and monthly summaries include demo history immediately. Print / PDF opens the browser print dialog; choose Save as PDF there.

To use physical devices only, set `DEMO_DATA=false` in `.env` and restart. Existing demo records remain labeled in history; they are not deleted. The optional `python -m simulator` is only for independent HTTP protocol testing with devices registered as `simulator`; adopted built-in demo rover IDs do not accept device telemetry.

`instance/` contains the database, local secret, operator credentials and simulator provisioning keys. It is ignored by Git. Keep it private and back it up; do not publish it.

## ESP32 camera

Open **Rover fleet → ESP32 Camera** to see the latest camera images, live/offline status and an expandable view. The camera captures 320 x 240 RGB565 and encodes JPEG at quality 88 with 4:4:4 chroma before uploading over persistent HTTP or certificate-verified HTTPS. This avoids transferring 153,600 raw bytes per frame across the internet. With PSRAM, encoding overlaps uploads using bounded latest-frame buffers; stale waiting frames are replaced rather than queued. The 10 fps target is not a measured guarantee; the viewer and Serial Monitor show actual rates. Camera traffic remains independent of sensor telemetry. The generated sketch and flashing instructions are in [docs/ESP32_CAMERA_SETUP.md](docs/ESP32_CAMERA_SETUP.md). Wi-Fi credentials and the configured sketch stay under ignored `instance/`.

## Architecture

```text
Browser (Jinja / CSS / vanilla JS / bundled Chart.js)
  | session-authenticated REST + CSRF for mutations
  | authenticated SSE notifications + periodic REST fallback
  v
Flask API -> strict validation -> ingestion transaction -> SQLAlchemy database
                                     |                 (SQLite / PostgreSQL)
                                     +-> rules + inspection events + change log
Built-in demo feed (5 s) --------------> persisted demo readings
Presence monitor (5 s) -----------------> stale/offline conditions + command expiry
  ^
  | public device REST API with independent per-rover keys
  |
simulator/ (one worker + HTTP session per rover; NO database/backend imports)
or real ESP32 firmware
```

```text
app.py                       Waitress entry point and presence monitor lifecycle
agrisentinel/__init__.py      Application factory, initialization, security headers
agrisentinel/models.py        Normalized SQLAlchemy schema and UTC type
agrisentinel/seed.py          Idempotent schema-v1 bootstrap; no telemetry seeds
agrisentinel/auth.py          Operator sessions, CSRF, device key verification
agrisentinel/validation.py    Strict version-1 payload validation
agrisentinel/api.py           Versioned REST routes and durable-change SSE
agrisentinel/services/       Built-in demo, alerts, presence, serialization, aggregation
agrisentinel/templates/      Jinja login and dashboard shell
agrisentinel/static/         Design system, frontend, fonts and chart library
simulator/                   Standalone HTTP rover simulator
tests/                       Isolated API, persistence, concurrency and simulator tests
scripts/smoke_test.py         Real two-process HTTP integration check
scripts/check_frontend.mjs   Optional frontend DOM integration check
ROBOT_DESIGN_REQUEST.md       Reusable physical rover/CAD/electrical/firmware brief
```

Hardware communication is independent of dashboard rendering. The simulator never imports the Flask package, SQLAlchemy or SQLite. Real hardware uses the same endpoints; register it as `hardware` and replace the virtual device process. The frontend and telemetry schema do not change.

### Database initialization and durability

Startup creates missing schema-v1 tables and indexes and records `Setting.schema_version = 1`. Repeated initialization preserves devices, measurements, thresholds, alerts, commands and inspections. A database with a different schema version is rejected. `create_all` is initialization, **not a general schema migration engine**; future schema changes must ship a versioned migration before changing the schema version.

- `Field` -> `Zone` -> assigned `Robot`; multiple rovers can share a partition.
- `Robot` holds hashed API credentials, server-owned source metadata, model, firmware, heartbeat and latest device state. Keys are not stored in plaintext in the database.
- `Telemetry` retains the immutable event ID, content fingerprint, measurement and receive timestamps, source, nullable sensor columns and reported state.
- `AlertRule`, `Alert`, `ActiveCondition` and `AlertTransition` retain thresholds, one open condition slot and its acknowledgement/resolution history.
- `Command` records queued, completed, failed, expired or cancelled requests.
- `Inspection` records device-reported inspection checkpoints linked to their telemetry events.
- `ChangeEvent` is a bounded notification log; telemetry and alert histories are retained.

Historical queries use `(robot_id, recorded_at)`, `(zone_id, recorded_at)` and time indexes. Raw telemetry uses cursor pagination, default 50 and maximum 500 records per response. Alert and inspection detail lists are bounded. Aggregation scans indexed time ranges in batches of 1,000 using bounded accumulator memory and emits at most 90 time buckets per zone. Query periods are capped at 31 days and comparisons at 12 zones. For very large fleets, add database-side rollups and retention policies based on measured workload.

Default storage is `instance/agrisentinel.db`, with SQLite foreign keys, WAL and a 10-second busy timeout. SQLite writes are serialized before ingestion and presence evaluation; duplicate concurrent retries are tested. Timestamps are normalized to UTC at the persistence boundary and serialized with `Z`.

For a consistent SQLite backup while the server is running, use Python's SQLite backup API or SQLite's `.backup` command. When copying files manually, stop the server first; do not copy just the main database while a live WAL may contain uncheckpointed changes. Preserve the session secret and provisioning configuration separately. Never delete `instance/` to perform an upgrade.

For PostgreSQL:

```powershell
.\.venv\Scripts\python.exe -m pip install "psycopg[binary]>=3.2,<4"
# Set DATABASE_URL in .env:
# postgresql+psycopg://agrisentinel:YOUR_PASSWORD@127.0.0.1/agrisentinel
```

The schema and queries use portable SQLAlchemy types and constraints, with PostgreSQL row locks and SQLite-specific locking isolated at the transaction boundary. PostgreSQL requires a running server/database; it was **not integration-tested** in this local delivery.

## Telemetry contract

See [the full sample payload](docs/telemetry-example.json) and [the validation implementation](agrisentinel/validation.py).

```http
POST /api/v1/telemetry
Authorization: Bearer <per-rover-api-key>
Content-Type: application/json
```

The exact required top-level fields are `schema_version`, `event_id`, `robot_id`, `zone_id`, `recorded_at`, `uptime_ms`, `telemetry` and `robot`. Unknown fields are rejected, including client-supplied `source`. `schema_version` must be integer 1. `recorded_at` must include `Z` or an offset and fall within the 30-day past / 120-second future window. `uptime_ms` is a nonnegative integer. Maximum request size is 16 KiB.

Sensor fields can be omitted or null; `telemetry` must contain at least one recognized key, including a null key for a heartbeat with unavailable sensors. Missing values stay unknown and are excluded from statistics. Raw fields must be integers, not calibrated physical concentrations.

| Sensor | Valid ingestion range | Unit |
| --- | --- | --- |
| `temperature_c` | -40 to 85 | °C |
| `humidity_pct` | 0 to 100 | % |
| `soil_moisture_pct` | 0 to 100 | documented calibrated scale |
| `mq2_raw`, `mq3_raw`, `sound_level_raw` | integer 0 to 4095 | raw ADC |
| `heading_deg` | 0 inclusive to 360 exclusive | degrees |
| `tilt_deg` | -180 to 180 | defined signed tilt axis |
| `obstacle_distance_cm` | 0 to 1000 | cm |

`robot.status` is `idle`, `patrolling`, `inspecting`, `stopped` or `error`. `battery_pct` is nullable, range 0–100. `checkpoint_id` is nullable or a configured checkpoint (`cp-01`..`cp-08` initially).

```powershell
# Use this rover's actual key from local provisioning or one-time registration.
# Update recorded_at to a valid current UTC timestamp and use a new event_id.
curl.exe -X POST http://127.0.0.1:5000/api/v1/telemetry `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer YOUR_ROVER_KEY" `
  --data-binary "@docs/telemetry-example.json"
```

| HTTP | Meaning |
| --- | --- |
| 201 | New telemetry persisted, rules evaluated and live notification queued |
| 200 | Exact duplicate retry; no extra record, inspection or alert |
| 401 | Missing/incorrect per-device API key |
| 403 | Rover does not own the reported zone |
| 409 | Existing event ID with different content |
| 413 / 415 | Request too large / wrong content type |
| 422 | Invalid JSON structure, data type, timestamp or measurement |
| 503 | Database unavailable; safe to retry the unchanged event |

Duplicate protection is global on `event_id`, with content comparison. Retrying an accepted event after reassignment still returns duplicate success. Late records are stored historically but cannot replace a newer device state or open/resolve current sensor conditions. Exact duplicate retries do not refresh the heartbeat. Server-received time establishes device contact; device-recorded time establishes measurement freshness.

## REST API and authentication

All dashboard data endpoints require an operator session. Mutations also require `X-CSRF-Token`, obtained from the dashboard's `csrf-token` meta element. Device keys only authorize that device's ingestion and command polling/acknowledgement; they do not grant dashboard access. Registration returns a key once, and subsequent reads never include keys or key hashes.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/v1/health` | Minimal unauthenticated health check |
| `GET /api/v1/overview` | Actual fleet counts, configured fields/zones, latest data, activity and alerts |
| `GET, POST /api/v1/robots` | List / register rover; registration source is `hardware` or `simulator`; built-in devices use server-owned `demo` |
| `GET, PATCH /api/v1/robots/<id>` | Details / update `zone_id` (or null to unassign) |
| `GET /api/v1/zones`, `/zones/<id>` | Partition metadata, readings, inspection coverage and assignments |
| `GET /api/v1/telemetry` | Paginated raw history; `robot_id`, `zone_id` or comma-separated `zones`, `period`, `before_id`, `limit` |
| `GET /api/v1/analytics` | Nine measurements; `period`, `zones`, `robot_id`, optional `from` / `to` |
| `GET /api/v1/alerts` | `status=open/active/acknowledged/resolved/all`, `zone_id`, cursor pagination |
| `POST /api/v1/alerts/<id>/acknowledge` | Acknowledge; does not claim measured recovery |
| `GET /api/v1/rules` | Current configurable environmental rules |
| `PATCH /api/v1/rules/<id>` | Update trigger, recovery, severity or enabled flag |
| `POST /api/v1/robots/<id>/commands` | Operator queues `patrol`, `inspect`, `stop` or `idle`; returns 202 |
| `GET /api/v1/robots/<id>/commands` | Device-key-authenticated poll; includes current zone assignment |
| `POST /api/v1/robots/<id>/commands/<command_id>/ack` | Device reports `completed` or `failed`, optional note |
| `GET /api/v1/reports` | Daily/weekly/monthly summary of stored data |
| `GET /api/v1/reports/export.csv` | Measurement statistics, trends, zone comparisons, activity, alerts and inspections |
| `GET /api/v1/events` | Operator-authenticated SSE; `ready`, `update`, `tick` events |

Analytics periods: `1h`, `6h`, `24h`, `7d`. Report periods: `daily` (24 hours), `weekly` (7 days), `monthly` (30 days), all rolling UTC windows. Reports support a chosen end time and zone. APIs also accept explicit ISO `from` / `to` within 31 days. CSV is available from the report view; **Print / PDF** uses the browser's print-to-PDF support and dedicated print styles, rather than a server PDF dependency.

Reports include valid-sample minimum, maximum and average; per-zone temperature trends; raw sensor units; source counts; alert conditions overlapping the selected window; and reported checkpoint inspections. Heading uses a circular mean (359° and 1° average around north, not south). Rover state counts are sample counts, not inferred operating hours. No missing sensor, uninspected plot or unknown crop condition is labeled healthy.

Commands expire after five minutes and have a maximum pending queue of ten per rover. Device state changes only when telemetry reports it. Reassignment cancels pending commands and retires the previous zone's open sensor conditions with an explicit history note; it does not claim sensor recovery. The simulator reads new zone ownership on its next command poll.

SSE uses a small durable change log, not process-local payload queues, and never transmits device credentials. The browser refetches current backend data after events; timed REST refresh continues if SSE disconnects. Presence evaluation runs independently of browser visits. Up to 12 concurrent SSE streams are admitted per server process; extra tabs retain REST fallback. Use one backend process for this local configuration.

## Alert behavior

| Condition | Opens | Recovers | Severity |
| --- | --- | --- | --- |
| Soil moisture | below 25% | at least 29% | warning |
| Temperature | above 36°C | at most 34°C | critical |
| Humidity | above 85% | at most 80% | warning |
| Nearby obstacle | below 20 cm | at least 30 cm | critical |
| Absolute tilt | above 18° | at most 12° | critical |
| Unusual MQ2 / MQ3 raw | above 3500 ADC | at most 3300 ADC | warning |
| Rover error | reports `error` | fresh non-error state | critical |
| Telemetry stale | sample age >30 s | newer sample within timeout | warning |
| Rover offline | last contact >45 s | new accepted device contact | critical |

Environmental rules are editable in **Alert configuration**, with recovery bands validated on the server. Missing or stale sensor values never resolve environmental conditions. A database-enforced active-condition slot prevents duplicate open alerts for a rover/zone/rule. Acknowledgement preserves that slot; recovery creates a resolution transition. A later recurrence creates a new alert. Disabling a rule retires its active conditions with an explicit administrative note. Newly registered devices without any contact are shown as awaiting contact; presence alerts begin after the first real heartbeat.

These thresholds are demonstration/operational settings, **not validated agronomic recommendations**. MQ2/MQ3 are raw readings and are never presented as concentrations or disease indicators.

## Simulator configuration

This optional legacy tool is not needed for the built-in demo. Use a device-only database with simulator-source devices for these checks. The default file `instance/simulator.json` is generated once during initial farm provisioning. Each listed rover has its own key. The service reads that file and communicates exclusively through public REST endpoints. There is no direct database insertion or Flask import.

```powershell
# Normal continuous mode
.\.venv\Scripts\python.exe -m simulator
# One event per rover, useful for inspection/testing
.\.venv\Scripts\python.exe -m simulator --once
# Alternate independent configuration
.\.venv\Scripts\python.exe -m simulator --config path/to/private-config.json
```

See [config.example.json](simulator/config.example.json) for optional ranges and network failure injection. Store real keys only in an ignored private file. To add more simulated devices after first boot, register each as `simulator` in the dashboard, save its one-time API key and add the roster entry to the private simulator configuration. `SIM_ROVER_COUNT` selects from the available provisioned roster; it cannot invent unregistered credentials. On the first backend launch it can provision 1–6 devices.

| Environment variable | Purpose / default |
| --- | --- |
| `SIM_CONFIG` | Private JSON config path; default `instance/simulator.json` |
| `SIM_API_URL` | Backend base URL; default config URL |
| `SIM_ROVER_COUNT` | Number of roster entries to run; default all configured |
| `SIM_API_KEY` | Optional override for a single-rover setup; use per-rover JSON keys for fleets |
| `SIM_INTERVAL_SECONDS` | Sending interval; default 10 |
| `SIM_TIMEOUT_SECONDS` | HTTP timeout; default 5 |
| `SIM_SEED` | Reproducible sensor/state random seed; default 42 |
| `SIM_ANOMALY_FREQUENCY` | Per-cycle anomaly start probability; default 0.08 |
| `SIM_OBSTACLE_FREQUENCY` | Per-cycle obstacle encounter probability; default 0.06 |
| `SIM_NETWORK_FAILURE_RATE` | Injected telemetry-request failure probability; default 0 |
| `SIM_ZONE_ASSIGNMENTS` | Initial `rv-001:zone-a,rv-002:zone-b` assignments; server remains authoritative |

Temperature follows a daily solar target with bounded steps; humidity is inversely correlated. Soil slowly dries with heat, with explicit simulated drought/watering patterns. Gas/sound raw signals drift around reproducible baselines. Multi-cycle anomalies introduce heat, humidity, drying or unusual MQ2 ADC values. Obstacles temporarily override motion; automatic patrol/inspect/idle phases resume afterward unless an operator command establishes a persistent state. Battery stays null because it is not modeled. Changing the sending interval accelerates or slows per-cycle demonstration dynamics.

Each worker has independent random state, credentials, HTTP connection and retry loop. Event IDs combine rover ID, a fresh boot UUID and sequence, while sensor values are reproducible for a given seed, timestamp and sequence. Retries reuse the exact same event and use exponential backoff with jitter, capped around 60 seconds. Permanent 4xx rejections are logged rather than retried as if delivery succeeded. Ctrl+C interrupts waits and shutdown completes after in-flight request timeouts.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use isolated databases and cover authentication, strict types/ranges/timezones, source integrity, missing sensors, duplicate/conflicting events, concurrent retries, ownership, out-of-order history, alert deduplication/hysteresis/recovery, stale/offline evaluation, acknowledgements, rules, commands, report/CSV calculations, circular headings, pagination, SSE, restart persistence and simulator/API compatibility. They do not modify local provisioning keys.

For legacy simulator protocol checks, use a separate device-only instance (`DEMO_DATA=false`) with simulator-source devices and the continuous simulator stopped:

```powershell
.\.venv\Scripts\python.exe scripts/smoke_test.py --offline
```

This launches the **real separate simulator process**, verifies HTTP ingestion, device commands, acknowledgements, inspections, analytics, alerts and CSV, then waits for the real offline timeout. It adds clearly identified simulated records and command history to the local instance and does not remove data. Disable other simulators during the offline check.

Optional frontend execution checks (Node 24; **Node is not required to run the product**):

```powershell
npm ci
npm run test:ui
```

These run the actual frontend against live authenticated HTTP responses in a DOM environment, exercising navigation, field selection, rover details, registration controls, sensor tabs, empty comparison selection, rules, report generation and CSV. Charts are instrumented for their input data, not visually rasterized. This is **not a substitute for visual/responsive browser QA**. Browser automation was unavailable in the implementation environment; desktop/mobile rendering and print layout remain to be visually reviewed in a real browser.

## Deployment notes

`python app.py` uses Waitress rather than Flask's development server, binds to loopback by default and starts the required presence monitor. There is no debugger. [Flask's deployment documentation](https://flask.palletsprojects.com/en/stable/deploying/index.html) explains why the development server should not be used publicly.

For a public installation, configure `APP_ENV=production`, an explicit long `SECRET_KEY`, `ADMIN_PASSWORD`, a private database, and `SESSION_COOKIE_SECURE=true`. Terminate HTTPS at a trusted reverse proxy, disable proxy buffering on `/api/v1/events`, and allow long SSE connections. Keep forwarded-header trust limited to your proxy configuration. The local deployment has one operator role; a larger organization should add named users/SSO, shared login rate limits, an audit policy, tested PostgreSQL deployment and a dedicated background worker for horizontally scaled presence evaluation. The current system intentionally avoids a task queue or message broker for the local two-process workflow.

Local login attempts are rate-limited in memory (10 failures/attempts per five minutes per client address); session mutations use CSRF protection, cookies are HTTP-only/SameSite, and static assets are served under a restrictive same-origin CSP. Treat simulator keys as device credentials even though their observations are simulated.

Chart.js and IBM Plex font licenses are bundled beside their assets. To deliberately refresh pinned vendor assets, run `python scripts/vendor_assets.py`; ordinary startup and dashboard use do not contact those sources.

## Physical robot design brief

[ROBOT_DESIGN_REQUEST.md](ROBOT_DESIGN_REQUEST.md) is a reusable request for the future AgriSentinel chassis, CAD, electronics, sensors, power budget, firmware, navigation boundaries and acceptance tests. It preserves this system's telemetry/command contract and distinguishes measured hardware capability from design targets.

## Farm chatbot and voice mode

Set `DEEPINFRA_API_KEY` in `.env` and restart `python app.py`. Open **Chatbot** in the sidebar or `/chatbot` directly. A modal requires choosing **English, Hindi, Marathi, Gujarati, Tamil, Telugu or Bengali** on first entry. **Change language** reopens it. Server-owned instructions require concise replies in the selected language. Indian-language output must contain its native script before it can begin streaming. Messages appear immediately with a thinking indicator; only answer deltas are streamed. Reasoning fields and inline reasoning tags are filtered, including split tags. Interrupted answers remain visible and can be retried. Disable reverse-proxy buffering for `/api/v1/chat` as well as `/api/v1/events`.

**Voice mode** replaces the conversation area with a circular audio visualizer and starts listening. After about 650 ms of silence, the question is sent automatically, with a **Send now** option and a 20-second maximum turn. Listening resumes after the reply. **Pause microphone**, **Interrupt**, **Change language** and **Back to chat** remain visible. Leaving Chatbot or hiding the tab releases the microphone. Actual microphone and playback analysers drive the visualizer, with reduced motion respected. This requires a supported browser with AudioWorklet over **HTTPS or localhost**; plain HTTP on a LAN IP cannot record.

AudioWorklet captures PCM directly and produces mono 16 kHz WAV for DeepInfra Whisper, avoiding a recording-decode round trip. A smaller Qwen3.5 model generates short voice answers with reasoning disabled. Sentence-level speech synthesis begins during text generation, with up to two synthesis requests in flight. Web Audio plays normalized mono 24 kHz PCM as it arrives. The server strips WAV containers from Inworld's streamed packets; it does not wait for a complete utterance.

Text uses `DEEPINFRA_CHAT_MODEL` (Qwen3 Instruct by default). Voice uses `DEEPINFRA_VOICE_CHAT_MODEL` (Qwen3.5-35B-A3B), `DEEPINFRA_STT_MODEL` (Whisper), `DEEPINFRA_FAST_TTS_MODEL` (English/Hindi) and `DEEPINFRA_INDIC_TTS_MODEL` (other Indian languages). Both streaming speech defaults are `inworld-ai/realtime-tts-2` through DeepInfra. Optional Kokoro support is available for English/Hindi, but measured first-audio latency was higher. The legacy non-streaming speech endpoint still uses `DEEPINFRA_TTS_MODEL`. All calls incur provider usage, and speech model prices differ. Actual latency and voice/language accuracy depend on provider availability, network conditions and the speaker.

Each question gets a new server-built snapshot of latest rover/zone readings, freshness, simulator/hardware labels, today's UTC inspection coverage, alert rules, up to 30 recent alerts, 20 commands and 24 telemetry samples. This is a bounded operational snapshot, not a full historical query engine. The assistant explains observations and suggests checks; rover commands and configuration remain operator actions through the existing dashboard. For longer-term history use Analytics or Reports.

The API key remains server-side. Conversation text, relevant farm data and recordings go to DeepInfra. History stays in tab memory and clears on reload or **New conversation**; audio is processed in memory. Requests require an operator session and CSRF token. Uploads are validated as short PCM WAV files. Errors omit provider bodies and secrets. Up to four AI requests run concurrently per server process. Streaming requests have a 45-second provider read timeout. Provider failures do not affect monitoring.

Provider references: [streaming](https://docs.deepinfra.com/chat/streaming), [reasoning controls](https://docs.deepinfra.com/chat/reasoning), [Inworld through DeepInfra](https://deepinfra.com/inworld-ai/realtime-tts-2), [language support](https://docs.inworld.ai/tts/capabilities/multilingual).
