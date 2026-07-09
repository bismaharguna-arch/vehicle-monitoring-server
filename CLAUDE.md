# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> A second, more narrative copy of this guidance lives at [catatan/CLAUDE.md](catatan/CLAUDE.md). Keep the two in sync when you change architecture-level facts. Most code comments and design docs in this repo are written in Indonesian — match that when editing.

## Stack & Environment

Flask 3.1 + Flask-SocketIO + Flask-Login, SQLAlchemy 2.0 (MySQL via PyMySQL/Laragon), Jinja templates. Frontend: Tabler (Bootstrap 5) + custom `static/css/refined.css` design system + HTMX + Alpine.js + Chart.js + SweetAlert2.

Windows-first project. Shell helpers are `.bat` files. Python venv at `venv/`. Config comes from `.env` (`SECRET_KEY`, `DATABASE_URL`, `DETECTOR_PREVIEW_URL`). App factory pattern: `create_app()` in `app.py`, run via `socketio.run(...)` on `0.0.0.0:5000`.

## Common Commands

| Command | Purpose |
|---|---|
| `start.bat` | Activate venv, install deps if missing, run `app.py` (server on `0.0.0.0:5000`) |
| `venv\Scripts\pip install -r requirements.txt` | Install/refresh dependencies |
| `tools\test-db.bat` | Sanity check: insert 1 dummy `Detection` row, confirm DB connectivity |
| `tools\seed.bat` | **DESTRUCTIVE** — `db.drop_all()` + reseed users + random detections. Requires typing `YES` |
| `tools\simulate.bat` | Long-running HTTP POST loop to `/api/detections` simulating an AI camera (includes failure-mode payloads). Server must be running |
| `venv\Scripts\python tools\migrate_fuel_enum.py [--apply]` | Migrate legacy English `is_electric` (`electric`/`gasoline`) → canonical (`listrik`/`bensin`). Default dry-run, idempotent |
| `venv\Scripts\python tools\migrate_add_detected_at.py [--apply]` | Add `detected_at` column to old `detection` tables. Idempotent |
| `venv\Scripts\python tools\migrate_add_corrected_by.py [--apply]` | Add `user_id` FK (correction audit trail) to old `detection` tables. Default dry-run, idempotent |
| `venv\Scripts\python tools\migrate_add_vehicle_masters.py [--apply] [--drop-plate]` | **#4 migration**: rename `detection`→`deteksi` + Indonesian columns, create `mobil`/`motor` masters, backfill from plates, link FKs. Needs MySQL 8+. Dry-run default, idempotent |

Default seeded credentials: `admin/admin123` (created automatically on boot in `app.py`). Guest (read-only) accounts are created via public self-registration (`/register`).

**There is no linter, no build step, and no `pytest` suite.** Testing is done with standalone HTTP/Socket.IO integration scripts in `tools/` run by hand against a live server (see [Testing](#testing)).

## Architecture

### Blueprint layout (`routes/`)

| Blueprint | Mount | Responsibility |
|---|---|---|
| `dashboard` | `/`, `/dashboard` | Main monitoring view (KPIs, trend chart, live feed, recent activity) |
| `riwayat` | `/riwayat` | History page: filterable + **paginated** detections table (25/page) |
| `auth` | `/login`, `/register`, `/logout`, `/api/profile` | Login, public self-registration (role forced `guest`), self-service profile edit (own username/password, never role) |
| `user_mgt` | `/users`, `/api/users*` | Admin-only account CRUD |
| `api` | `/api/detections*`, `/api/stats`, `/video_feed` | **Public IoT contract** (POST) + role-gated edit/delete |

### Realtime data flow

External camera (or `tools/simulate_realtime.py`) → `POST /api/detections` → `db.session.commit()` → `socketio.emit('new_detection', payload)` → browser.

The **`base.html` bridge** (search `socket.on('new_detection'`) translates the Socket.IO event into an HTMX trigger. Elements with `data-vm-refresh-on="new_detection"` and `hx-trigger="vm:refresh from:body"` refresh automatically via partial routes — that's how KPIs, recent table, and history table stay live without bespoke JS.

**Polling fallback (`every 10s`):** every HTMX target that listens for Socket.IO also has `every 10s` appended to its `hx-trigger` (pattern `hx-trigger="vm:refresh from:body, every 10s"`), so data stays fresh within 10s even if the Socket.IO event is lost.

### Detection contract (v1)

A companion detector at `D:\deteksi_kendaraan` POSTs to `/api/detections`. The endpoint is **public** (no `@login_required`) so IoT devices can POST without a session.

Body is `multipart/form-data` (preferred) or `application/json`:

| Field | Required | Notes |
|---|---|---|
| `plate_number` | no | May be `""`/`null` (OCR off). UI renders `—` |
| `vehicle_type` | yes | Canonical **`mobil` \| `motor` \| `unknown`**. Legacy English (`car`/`motorcycle`) coerced + logged |
| `is_electric` | yes | Canonical **`listrik` \| `bensin` \| `unknown`**. Legacy English (`electric`/`gasoline`) coerced + logged |
| `confidence_score` | yes | YOLO score 0.0–1.0. UI badges "Low" when `<0.65` |
| `detected_at` | no | ISO 8601 TZ-aware. Server converts to naive local. Skew >±1h → log warning. Invalid → NULL |
| `photo` | no | JPG/PNG. `MAX_CONTENT_LENGTH = 5 MB` (`app.py`) — over limit → `413` JSON |

Enum normalization lives in `routes/api.py`: `_normalize_fuel`, `_normalize_vtype`, `_parse_detected_at`. **All KPI count queries must filter on canonical values (`listrik`/`bensin`), not English.**

**Input validation on POST (returns `400`/`413`/`415`, never 500/503 for bad input):** `vehicle_type` & `is_electric` must be **present** (absent/empty → `400`; unrecognized *value* is still coerced to `unknown`). `confidence_score` must parse to float (else `400`) and is clamped to `[0,1]`. `plate_number` > 32 chars → `400` (matches master `plat_nomor` width). Photo must be `.jpg/.jpeg/.png/.gif/.webp` (else `415`); filename capped to 150 chars; over `MAX_CONTENT_LENGTH` → `413` (the route re-raises `RequestEntityTooLarge` so the global handler answers 413 instead of the broad `except` swallowing it as 500).

**Timestamp model:** `timestamp` column = DB row creation (de-facto `received_at`); `detected_at` = detector-side time. UI shows `detected_at ?? timestamp`.

**Late-OCR correction (`PATCH /api/detections/<id>/plate`):** detector-facing follow-up when the plate is read *after* the initial push. Auth = same `X-API-Key` as POST (`_check_api_key()` called manually — no `@login_required`). JSON body: `plate_number` and/or `is_electric` (at least one → else `400`; plate > 32 chars → `400`). Applies `_normalize_fuel` + re-runs `_link_master`, but **never touches the correction audit trail** (`sudah_dikoreksi`/`dikoreksi_oleh`). If the record was already corrected by an admin → `200 {"status":"SKIPPED"}` (human correction wins; 200 so the detector doesn't retry). Distinct from the admin `PATCH /api/detections/<id>` (session + role-gated) — don't merge the two. No Socket.IO emit; HTMX 10s polling picks up the change.

**Camera preview:** `/video_feed` (server-side `cv2.VideoCapture(0)`) exists in `routes/api.py` but is **not wired into templates** because it conflicts with the detector also holding the webcam. The dashboard instead embeds the detector's own MJPEG `/preview` via `<img>`, URL from env `DETECTOR_PREVIEW_URL` (default `http://localhost:5001/preview`; empty = disabled). Alpine `cameraFeed()` auto-retries every 5s.

### DB offline graceful degradation

`app.py` registers errorhandlers for SQLAlchemy `OperationalError`/`InterfaceError`/`DBAPIError`. When MySQL is down:
- `/api/*` → `503 JSON` with `code: "DB_UNAVAILABLE"`
- HTML routes → render `templates/db_offline.html` (503)
- `load_user()` is wrapped in try/except and reconstructs a ghost user from session cache so pages still render read-only instead of force-logging-out
- `db.create_all()` + seeding at boot is wrapped in try/except — server stays up and recovers automatically once DB returns

### Two route flavors

- **JSON API routes** (`/api/...`) — consumed by IoT devices and direct fetch (edit/delete/profile). Contract must stay stable.
- **Partial routes** (`/partials/...`) — return HTML fragments from `templates/partials/_*.html`, consumed by HTMX `hx-get`. Add new HTMX-driven sections here; **do not mix HTML rendering into JSON API routes.** Each partial is also `{% include %}`-d on initial render to hydrate without flicker.

**History pagination (`routes/riwayat.py`):** `PER_PAGE = 25`. HTMX swaps the whole `<div id="vm-history-result">` wrapper (`hx-swap="innerHTML"`). Do NOT emit bare `<tr>` + a sibling `<div>` in one response — the HTML parser foster-parents the `<div>` out of the table and breaks the swap. Page survives `every 10s` refresh via hidden `#vm-page-input`; filter changes call `vmResetPage()`.

**History filters/sort/export (`routes/riwayat.py`):** filters are date, plate, type, fuel, **hour range** (`jam_start`/`jam_end` → `TIME(timestamp)`), and **date range** (`date_from`/`date_to`, used by the print dialog). The **plate filter JOINs the master tables** (`outerjoin Mobil/Motor` + `or_(... .ilike ...)`) since plate moved to master; type/fuel still filter the `deteksi` fallback columns. **Tipe & Jenis are header dropdowns**, not sidebar fields — their value lives in hidden `#vm-type-input`/`#vm-fuel-input` (single source of truth); the `<select>` in the table header calls `vmHeaderFilter()` which sets the hidden input and triggers refresh. **Column sorting is Task-Manager style** but now only `SORT_COLUMNS` = waktu/conf are sortable (click header → asc, click again → desc); Plat/Tipe/Jenis are NOT sortable. Sort state lives in hidden `#vm-sort-input`/`#vm-dir-input` so it survives polling. `_query_from_args()` is the shared filter+sort builder used by pagination, **CSV export** (`/riwayat/export.csv`, serializes the live filter form), and **printable report** (`/riwayat/cetak` → standalone `cetak_riwayat.html`, auto `window.print()`). The **Cetak button opens an Alpine dialog** (`openPrint`/`doPrint`) where the user picks date range + tipe + bahan bakar before generating the report; CSV stays a one-click export of the current view.

### Data model & Indonesian column names (post-#4)

DB tables/columns are mostly **Indonesian** (the codebase is Indonesian). The transaction model class is `Deteksi` (table `deteksi`, was `Detection`/`detection`). Two **master** tables `mobil` & `motor` hold distinct vehicles keyed by `plat_nomor` (unique) + `bahan_bakar`. `deteksi` references them via nullable FKs `id_mobil`/`id_motor` (exactly one set for plated detections; both NULL = plate-less/unknown).

Column rename map (old → current): `vehicle_type`→`tipe_kendaraan`, `is_electric`→`bahan_bakar`, `photo_filename`→`file_foto`, `detected_at`→`waktu_deteksi`, `is_corrected`→`sudah_dikoreksi`, `corrected_at`→`waktu_koreksi`, `user_id`→`dikoreksi_oleh`, `User.role`→`peran`. **Kept English:** `timestamp`, `confidence_score`, `username`, `password_hash`. **Dropped:** `deteksi.plate_number` (plate now lives in master) — access it via the `Deteksi.plat_nomor` **property** (derives from linked master; templates/JSON use `item.plat_nomor`). Filtering by plate must JOIN the master tables (`or_(Mobil.plat_nomor.ilike, Motor.plat_nomor.ilike)`), not the property.

**Normalization is pragmatic:** `tipe_kendaraan`/`bahan_bakar` are ALSO kept on `deteksi` as a synced fallback so (a) plate-less detections keep their AI classification and (b) KPI/stats queries stay simple (`filter_by(bahan_bakar=...)`). Only vehicle **identity** (plate) is fully normalized into the masters.

**HTTP contract stays English:** the detector POST keys (`plate_number`, `vehicle_type`, `is_electric`, ...), the `GET /api/detections` JSON, the Socket.IO `new_detection` payload, and the `/api/users` `role` key are all unchanged English — mapped to Indonesian columns in the route code. Don't rename these or the detector/JS breaks.

### Master upsert (`routes/api.py:_link_master`)

On `POST`/`PATCH`, a plated detection find-or-creates its master (`Mobil`/`Motor`) by `plat_nomor` and sets `bahan_bakar` (non-unknown latest wins). Plate-less or `unknown`-type → no master link. PATCH recomputes the link, so changing the plate re-links/creates a master and changing the type moves the FK between `mobil`/`motor`. The masters are the registry of **distinct vehicles** (answers "how many bensin cars" the advisor asked for), distinct from detection events. (A dedicated `/kendaraan` UI page existed briefly but was removed — surface distinct-vehicle counts via these master tables if needed again.)

### Role model & security gating

Two roles in `models.User.role`: `admin` (full access — PATCH corrections, DELETE, user management) and `guest` (read-only). Gating is enforced at **both** the template level (`current_user.role`) and the route level (`if current_user.role != 'admin': return 403`). Both layers must agree when adding new actions.

**Public entry point (no account needed):** a self-registration page (`GET/POST /register` — creates an account with role **forced to `guest` server-side**; username 3–60 chars unique, password ≥ 4 chars, auto-login on success). Registered (or admin-created) guest accounts can edit their own profile via `/api/profile`. The old "Masuk sebagai Tamu" button + shared `tamu` account were **fully removed** — self-registration is the only read-only path. `/login` & `/register` are served with `Cache-Control: no-store` (blueprint `after_request` in `routes/auth.py`) so the Back button after login can't show a stale cached auth page.

### Correction audit trail

`PATCH /api/detections/<id>` sets `sudah_dikoreksi=True`, `waktu_koreksi=now()`, and `dikoreksi_oleh=current_user.id`. The `Deteksi.pengoreksi` relationship (lazy-joined `User`) exposes the editor's username in `GET /api/detections` (JSON key stays `corrected_by`). New detections emit `is_corrected: False`; `dikoreksi_oleh` is NULL until first correction.

### File uploads

Detection photos go to `uploads/detections/`, served via the `uploaded_file` route in `app.py` (NOT Flask static, since they live outside `static/`). Filename pattern: `YYYYMMDD_HHMMSS_<microseconds>_<secure_filename>`.

### Frontend conventions

- **CSS namespace `vm-*`** in `static/css/refined.css` is the central design system. Don't override Tabler/Bootstrap globals — create/extend a `.vm-*` class.
- **Theming is 100% CSS-variable driven** (`--vm-*`): a `:root` (light) block and a `[data-bs-theme="dark"]` override block. To retheme, change variables, not component rules. Default theme is **dark** (`localStorage['vm-theme']`).
- **FOUC guard:** an inline `<script>` in the `<head>` of `base.html`, `login.html`, AND `db_offline.html` sets `data-bs-theme` before CSS loads — each standalone page needs its own copy (no shared head).
- **Liquid glass** = `backdrop-filter` + translucent bg. Gotcha: nested `backdrop-filter` renders muddy, so dropdown menus (`.vm-menu`) and modals (`.vm-modal`) are kept SOLID.
- **Alpine for state, HTMX for data** — modals/dropdowns/lightbox = Alpine `x-data`; tables/forms/KPIs that talk to the server = HTMX `hx-get`/`hx-trigger`. `base.html` wraps a global `x-data="vmGlobal()"` scope (owns the profile modal; trigger via `$dispatch('vm:open-profile')`).
- **Login page is standalone** — does NOT extend `base.html`.

### Critical Jinja + Alpine pitfall

`{{ value|tojson }}` produces double-quoted JSON, so the HTML attribute holding it MUST use single quotes:

```jinja
<button @click='openEdit({plate: {{ x|tojson }}})'>   {# correct #}
<button @click="openEdit({plate: {{ x|tojson }}})">   {# BROKEN — parser splits the attribute #}
```

Same rule applies to `onclick=`. See `_detection_rows_history.html`, `_user_rows.html`.

## Testing

No `pytest`. Tests are standalone HTTP/Socket.IO scripts in `tools/`, run by hand (often from a separate client laptop) against a running server. They depend on the seeded accounts and on an `API_URL`/`API_URL`-style constant near the top of each file — edit the IP when running cross-machine.

| Script | Verifies |
|---|---|
| `test_auth_role.py` | Login/logout, wrong-password + SQL-injection rejection, public IoT POST stays open, admin-only CRUD 403s for guest, no self-escalation |
| `test_string_attacks.py` (`.bat`) | Malformed input validation + response time. Writes a report |
| `test_file_upload.py` | multipart `photo`: happy path, 5 MB → 413, empty file, path traversal, fake MIME |
| `test_burst.py` | Sequential + concurrent load on `/api/detections`. Writes `test_burst_report.txt` + `.csv` (p50/p95) |
| `test_realtime_latency.py` | End-to-end POST→Socket.IO `new_detection` latency (target < 1s) |
| `test_db_offline.py` | 3-phase interactive: baseline → kill MySQL (expect 503) → restart (expect auto-recovery) |

Finished reports live in `test_report/`. `tools/bikin_grafik.py` (needs `matplotlib`) renders presentation PNGs from them.

Tools that import the app (`tools/test_insert.py`, `tools/dummy.py`) include a `sys.path.insert(0, ...)` bootstrap; their `.bat` launchers `cd` to project root so `load_dotenv()` finds `.env`.
