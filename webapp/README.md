# SE Agent Webapp

Frontend web application for interacting with the SE backend service to create and manage training decks.

Built with **Angular 16** (`ng build`). The production bundle is emitted to `dist/se-webapp/`
and served by nginx.

## Requirements

- Docker (recommended path — no local Node required)
- Node.js 20+ and npm 9+ (only for non-Docker local development)
- Backend service reachable from browser runtime

## Local Development

The app builds and runs entirely in Docker — no local Node needed. After **any** code
change, rebuild the image and recreate the container:

```bash
docker rm -f se-agent-webapp                                    # free the name + port 8080 (ignore "No such container" the first time)
docker build -t se-agent-webapp .                               # rebuild image with your changes
docker run -d --name se-agent-webapp -p 8080:80 se-agent-webapp # serve on http://localhost:8080
```

Open `http://localhost:8080` and **hard-refresh (Ctrl+Shift+R)** to drop cached assets, then sign in.

> Rebuilding the image does **not** update an already-running container, and `-p 8080:80`
> fails if another container already holds port 8080 — always `docker rm -f` first.

### Local Docker Compose

For local runs there is a dedicated `Dockerfile.local` and `docker-compose.local.yml` that
bake the local backend URL (`http://localhost:8002`) into the bundle at build time. This is
the **only** path that targets localhost — deployment builds use same-origin `/api` (see
below):

```bash
docker compose -f docker-compose.local.yml up --build -d   # build + serve on http://localhost:8080
docker compose -f docker-compose.local.yml down            # stop and remove the container
```

Override the backend without editing files by passing a build arg:

```bash
docker compose -f docker-compose.local.yml build --build-arg VITE_BACKEND_URL=http://host.docker.internal:8002
```

> `VITE_BACKEND_URL` is inlined at **build time**, so changing it requires a rebuild
> (`--build`). The browser runs on the host, so `localhost:8002` targets a backend on your machine.

## Environment Configuration

The backend base URL can be configured in the following order:

1. Query string override: `?backend=https://your-backend`
2. Saved value from the in-app `Server settings`
3. Build-time value baked into `environment.ts` (`VITE_BACKEND_URL` via `Dockerfile.local`)
4. Fallback default: `''` (same origin) — the app calls `/api` relative to its own host
   (`window.location.origin`), e.g. `https://rokassist-d.azure.rockwellautomation.com/api/...`
   on a deployment. The ingress routes `/api` to the backend, so no host is hardcoded.

**Deployment (CICD)** builds `Dockerfile-webapp` with the committed `backendUrl: ''`, so the
deployed app always talks to `/api` on its own origin — nothing to change per environment.
**Local development** bakes `http://localhost:8002` (see *Local Docker Compose* above).

## Production Build

```bash
npm run build
```

This runs `ng build` and generates static assets in `dist/se-webapp/`.

## Docker Deployment

The repository includes a production multi-stage Dockerfile:

1. Build stage: installs deps and runs `npm run build` (`ng build`)
2. Runtime stage: serves `dist/se-webapp/` using `nginx:alpine` on port `80`

Build image:

```bash
docker build -t se-agent-webapp .
```

Run container:

```bash
docker run --rm -p 8080:80 se-agent-webapp
```

Then open `http://localhost:8080`.

## QA Readiness Checklist

- `.gitignore` excludes caches, logs, env files, and build outputs
- `.dockerignore` excludes non-runtime files from Docker context
- No local cache directories committed
- App can be built with `npm run build` (`ng build`)
- Container can be built and started with Docker

## Analytics Dashboard

Analytics is available only to admins. The dashboard uses the existing authenticated
API client and backend configuration for these relative paths:

- `GET /analytics/dashboard`: overview, trends, adoption, performance, cost, quality,
   estimated value, agent/model breakdowns, and data-quality indicators.
- `GET /analytics/projects`: server-side sorting/pagination and export of all matching
   projects, not just the visible page.
- `GET /analytics/projects/{session_id}`: all-time workflow, agent-node, and document
   ingestion detail. The deprecated `/analytics/summary` is no longer requested.

The default window is the preceding 30 days. Custom dates are inclusive UTC calendar
dates; the frontend sends the day after the selected end date as the API's exclusive
`to`. The maximum range is 366 days. Display timezone changes timestamp labels, not
UTC aggregation buckets. Agent/model filters affect usage breakdowns only, not
overview totals or the project list. Project detail explicitly covers all recorded
activity, independent of the selected dashboard window.

Null values remain "No data"; ratios with a zero denominator are omitted. Measured,
derived, and estimated metrics are labelled, including ratio bases and each estimate's
confidence/baseline version. Missing pricing is surfaced instead of implying complete
cost coverage. No approval-cycle, historical comparison, or role-level capacity data
is fabricated when the API does not supply it.

Charts use Chart.js with shared theme tokens. Detailed tables remain available for
the chart data. The native project dialog supports focus trapping, Escape, and focus
restoration. CSV preserves null cells and escapes spreadsheet formula prefixes.
Denied requests clear analytics and hide its navigation; existing session refresh and
sign-out behavior handles expired authentication.

The AI usage chart immediately above the Projects table reuses that table's loaded
page; it makes no additional requests. Tokens, AI cost, and paired agent-processing /
AI-turnaround bars can be grouped by project or owner and are sorted locally.
KPIs and owner totals cover only chartable projects on the current page, not all
matching projects. As in the reference design, rows with missing cost/agent time or
zero/missing tokens are excluded and listed below the chart. Unknown owners remain
a separate group; missing turnaround values are not converted to zero. Pagination,
server sorting, and date changes update both views from the same response.

### Docker Verification

Run from this folder in PowerShell. No host Node/npm is needed. Recreate the frontend
so tests do not exercise a stale container:

```powershell
docker compose -f docker-compose.local.yml up --build -d --force-recreate
docker build -f Dockerfile.local --target build -t se-analytics-check .
docker run --rm -v "${PWD}:/work:ro" -e SMOKE_SCOPE=analytics-helpers -e NODE_PATH=/app/node_modules se-analytics-check node /work/scripts/component-refactor.smoke.cjs
docker run --rm -v se-analytics-pw:/tmp/pw mcr.microsoft.com/playwright:v1.51.1-noble npm install --prefix /tmp/pw playwright@1.51.1 --no-audit --no-fund
docker run --rm -v "${PWD}:/work:ro" -v "${env:TEMP}/se-analytics-artifacts:/artifacts" -v se-analytics-pw:/tmp/pw:ro -e NODE_PATH=/tmp/pw/node_modules -e SMOKE_SCOPE=analytics -e ARTIFACT_DIR=/artifacts mcr.microsoft.com/playwright:v1.51.1-noble node /work/scripts/component-refactor.smoke.cjs
```

The analytics smoke scope covers responsive layouts (320, 390, 768, 1024, 1440px),
both themes, rendered chart pixels, metric semantics, all three endpoints, filtering,
server paging/sorting, full CSV export, detail tabs/focus, HTTP 400/401/403/500,
empty states, and stale-request cancellation. Screenshots are written under
`$env:TEMP/se-analytics-artifacts`. Remove `-e SMOKE_SCOPE=analytics` to run the existing
cross-tab regression suite. All backend/auth/blob traffic is synthetic and blocked
from reaching live services; a real backend plus an admin session is still required
to verify live analytics at http://localhost:8080.

The legacy full smoke scope currently stops at its pre-existing `#user-chip` selector,
which is absent from the current profile-widget header. The analytics scope does not
depend on that selector and can be run independently.

### Rename and timestamp smoke tests

Playwright smoke tests for project rename ([RENAME_FUNCTIONALITY.md](RENAME_FUNCTIONALITY.md))
and creation-time labels ([TIMESTAMP_DISPLAY_CHANGES.md](TIMESTAMP_DISPLAY_CHANGES.md)).
They share `scripts/lib/project-smoke-harness.cjs`, which seeds `localStorage` and mocks
every `/api` call, so no backend is needed. The browser runs in `Asia/Kolkata` with
the `en-US` locale so expected times are deterministic. With the frontend serving on
8080 and the `se-analytics-pw` volume installed as above:

```powershell
docker run --rm -v "${PWD}:/work:ro" -v "${env:TEMP}/se-project-smoke:/artifacts" -v se-analytics-pw:/tmp/pw:ro -e NODE_PATH=/tmp/pw/node_modules -e ARTIFACT_DIR=/artifacts mcr.microsoft.com/playwright:v1.51.1-noble node /work/scripts/rename.smoke.cjs
docker run --rm -v "${PWD}:/work:ro" -v "${env:TEMP}/se-project-smoke:/artifacts" -v se-analytics-pw:/tmp/pw:ro -e NODE_PATH=/tmp/pw/node_modules -e ARTIFACT_DIR=/artifacts mcr.microsoft.com/playwright:v1.51.1-noble node /work/scripts/timestamps.smoke.cjs
```

- `rename.smoke.cjs` covers:
  - Rename from the sidebar pencil and the Pipeline **Rename** button.
  - The prompt opens prefilled.
  - Cancel, Escape, overlay click, a blank title, and an unchanged title make no request.
  - A trimmed `PATCH /api/decks/{id}/title` with a URL-encoded ID.
  - The server-returned title takes priority.
  - Updates to the active row, the stored title, and toasts.
  - Error toasts for 409 and 500 responses.
  - Persistence after reload.
  - Rename controls are disabled during deletion.
  - Hover color in both themes.
  - No stray comment text in the Pipeline row.
- `timestamps.smoke.cjs` covers:
  - The sidebar `status · vN · Mon DD HH:MM` labels and their tooltips.
  - Server `created_at` replaces stale local values; null or invalid dates are omitted.
  - Correction is read-only, with no writes to the backend.
  - The label stays on one line and uses the correct color in both themes.
  - Per-version stamps on output cards.
  - Persistence after reload.
  - New projects show `new · <now>`.

Failure artifacts (JSON API log, HTML, and screenshot) are written to
`$env:TEMP/se-project-smoke`.

`scripts/download-filename.smoke.cjs` uses the same harness and command, and also mocks
the SAS URL and Blob responses. It checks that downloaded decks are named
`{plan_title}_{projectTitle}_v{version}[_{language}].pptx`, where `plan_title` comes from the
`ppt_versions` row returned by `GET /decks/{id}/versions`. Coverage includes Studio output
downloads, Pipeline downloads for a project that is not open, stripping characters that are
not allowed in filenames, the `{projectTitle}_v{version}` fallback, and using the new title
after a rename.