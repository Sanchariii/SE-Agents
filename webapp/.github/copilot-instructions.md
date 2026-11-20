# Copilot Instructions — SE Agent Webapp

## Environment: NO local Node — build & test via Docker only
- Node.js / npm are **NOT installed** on this machine. Do **not** run `npm`, `npx`, `node`,
  or `vite` directly — they fail with "not recognized".
- The app is built and served through Docker (multi-stage: Node build stage → nginx runtime).
  Every build/test cycle goes through Docker.

## Build & test after ANY code change
Run from the repo root (`se-webapp/`), in PowerShell:

```powershell
docker rm -f se-agent-webapp 2>$null                            # free the name + port 8080
docker build -t se-agent-webapp .                               # rebuild image with the latest source
docker run -d --name se-agent-webapp -p 8080:80 se-agent-webapp # serve on http://localhost:8080
```

App runs at http://localhost:8080. After (re)starting, assume the browser needs a hard
refresh (Ctrl+Shift+R) because bundle assets are cached.

### Why `docker rm -f` first (do not skip)
- Rebuilding the image does **not** update an already-running container — it must be recreated.
- `docker run -p 8080:80` exits non-zero ("port is already allocated") if another container
  already holds 8080. A stale container silently keeps serving the OLD bundle, which looks
  like "my changes didn't apply".

## Verify a change actually shipped (don't trust the build log alone)
Bundle filenames are content-hashed, so grep inside the running container:

```powershell
docker exec se-agent-webapp sh -c "grep -c '/api' /usr/share/nginx/html/assets/index-*.js"  # expect >= 1
(Invoke-WebRequest http://localhost:8080/ -UseBasicParsing).StatusCode                        # expect 200
```

## Architecture & conventions
- Vanilla JS + Vite. Entry `src/main.js`, API client `src/api.js`, runtime config `src/config.js`.
- **All backend APIs live under the `/api` prefix** (e.g. `http://localhost:8002/api/...`),
  including `/api/health`.
- The `/api` prefix is centralized: `src/config.js` → `getApiBaseUrl()` returns
  `` `${getBackendUrl()}/api` ``; `api.base` is set from it. Add endpoints as **relative paths**
  (`/decks/...`, `/auth/...`) — never hardcode `/api` or an absolute host per call.
- Backend host default `http://localhost:8002`, overridable via `?backend=`, the in-app
  Server settings, or `VITE_BACKEND_URL` at build time.
- Ports: frontend `8080`, backend `8002`.

## Don't
- Don't run `npm install` / `npm run build` / `node` / `vite` locally (no Node runtime).
- Don't create or commit `dist/` — it is produced inside the Docker build and is git-ignored.
- Don't hardcode absolute backend URLs or per-call `/api` prefixes in `src/`.

## Theming & styling (light/dark mode) — centralized, do not bypass
All color, elevation and radius values live as CSS custom properties in
[src/style.css](src/style.css#L1), defined once in `:root` (dark, default) and mirrored in
`:root[data-theme="light"]`. The active theme is set via `data-theme="light"|(absent = dark)`
on `<html>`, toggled by the floating `#theme-toggle` button (wired in
[src/main.js](src/main.js#L1)) and persisted in `localStorage` under `se_theme`. A small
inline script in [index.html](index.html#L1)'s `<head>` applies the saved/OS-preferred theme
before first paint to avoid a flash.

**When adding any new element, component, or page:**
- Never hardcode a hex/rgb color, shadow, or border-radius in a rule — reference an existing
  variable instead (`var(--primary)`, `var(--text)`, `var(--muted)`, `var(--border)`, `var(--bg)`,
  `var(--panel)` / `var(--panel-2)` / `var(--panel-3)`, `var(--radius)` / `var(--radius-lg)`,
  `var(--shadow-sm)` / `var(--shadow-md)`).
- Use `--primary` / `--primary-hover` / `--primary-soft` for brand actions, links, active/selected
  states and focus rings (`box-shadow: var(--ring)`).
- Use `--danger` (not `--primary`) for destructive/error/failed states, `--green` for
  success/approved/done, `--yellow` for pending/warning, `--blue` for running/info, `--purple`
  for answered/secondary-info — each has a matching `*-soft` variant for tinted backgrounds/pills.
- If a genuinely new color is needed, add it to **both** the `:root` and
  `:root[data-theme="light"]` blocks in `src/style.css` so it works in both themes, then use the
  variable everywhere else.
- Reuse existing structural classes instead of inventing new ones: `.btn` / `.btn-primary` /
  `.btn-secondary` / `.btn-ghost`, `.field`, `.panel-header` + `.table-wrap` + `.data-table` for
  list/table pages, `.cards-grid` + `.stat-card` for metric cards, `.modal-overlay` + `.modal-card`
  for dialogs (see [src/ui.js](src/ui.js#L1)), `.toast` for notifications.
- Test new UI in both themes: toggle `#theme-toggle` (or set `localStorage.se_theme` to `"light"`
  / `"dark"`) and confirm contrast/readability in each.
