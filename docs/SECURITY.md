# Security

## Secrets

- **Where secrets live.** The Liara token, LLM keys and gate token, admin password, session secret, MCP, watchdog and status tokens, and the demo chaos token live only in:
  - Liara env vars of `negahban-sre`, set by `deploy/deploy_negahban.py`;
  - 600-mode files on the operator host: `/root/.liara.env`, `/root/.boxapi.env`, `/root/.negahban-admin.env`, `/root/.negahban-demo.env`.

  They are never in git. `.gitignore` excludes `.env*`, `data/` and `*.db`, and the source tarballs skip `.env*`.
- **The browser never sees a token.** All Liara and LLM calls are server-side.
- **Redaction** (`redact.py`):
  - Known secrets are replaced with `***`.
  - Pattern rules catch bearer tokens, JWTs, `sk-…` keys, `password=` / `token=` pairs and DB URLs with passwords.
  - Redaction applies to audit rows, SSE events, API responses and LLM prompts.
- **Env values are masked.** Liara returns env values in clear text. Values of sensitive-looking keys are masked (`•••xy`) before they reach the UI or the LLM.
- **Typed env values** (the `set_env` card) never enter the proposal row. They sit in the private key-value store under a reference id until the card is executed.
- **Env snapshots.** Negahban keeps the last-seen env values on its private disk so it can restore a deleted key. The snapshots are never displayed.

## Authentication and the web layer

- **Single owner login.** Password from `ADMIN_PASSWORD`, compared in constant time.
- **Session cookie.** Signed with HMAC-SHA256, 12 h expiry, `HttpOnly`, `Secure`, `SameSite=Strict`.
- **CSRF.** Every mutating endpoint requires a double-submit token (`ngh_csrf` cookie plus `X-CSRF-Token` header) and an Origin check.
- **Rate limits.**
  - Failed logins: 6 per IP and 40 overall per 5 min, plus a 0.8 s delay on failure.
  - Chat: 12 per minute per session.
  - Approvals: 20/min. Diagnostics: 10/min. Scenarios and project analysis: 6/min. MCP: 60/min.
- **Security headers.** Strict CSP (`script-src 'self'`, no inline script, no CDN), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`. All assets, including the font and the diagram renderer, are vendored.
- **No-cache.** `index.html` is served with no-cache and versioned asset URLs.
- **Escaping.** All server text is escaped before insertion into the DOM. Mermaid diagram SVG is sanitized (no script, foreignObject or `on*` attributes) before it is shown.
- **Machine endpoints.** `/mcp`, `/api/watchdog/ping` and `/api/status/ingest` each need their own bearer token. `/status` and `/healthz` are public and secret-free.

## Human in the loop

- **Risk classes.**
  - **read** and **safe** tools run automatically. Safe tools are diagnostics, sandboxed tests and patch preparation; none of them changes anything on Liara.
  - **sensitive** tools only ever create a card. Even the LLM cannot execute them. The agent prompt and the tool descriptions say so, and the code path enforces it (`tools.run` routes sensitive tools to `approvals.propose`).
- **Each card** shows what will happen, why, downtime, cost delta (toman/hour and per day), risk, deploy quota and a rollback plan. A card is:
  - **one-use**: an atomic DB state transition;
  - **expiring**: 20 min;
  - **bound to an app-state fingerprint**: plan, scale, current release, env keys. If anything changed, the card becomes `stale` and is refused.
- **Deleting an app** additionally requires typing the app name on the card.
- **Hard locks** (`write_lock`, `env_lock`) are enforced at execution time. Approval cannot override them.
  - `meowmeow-wallex` (real-money trading) and `negahban-sre` itself: write-locked and env-locked.
  - `bazargard-ai`: env-locked; its autopilot may only resize between standard-base-g2 and standard-plus-g2.
- **Autopilot** only does what the owner enabled per app: plan range, daily toman cap, cooldowns. Every automatic action is a normal card marked 🤖, auto-approved by `autopilot`, verified and audited with before and after metrics.

## LLM safety and prompt injection

- **Untrusted data.** Logs, HTTP bodies, build output, uploaded zips, git repos and web content are treated as untrusted. `sanitize.py`:
  - strips invisible, bidi, tag-block and variation-selector characters, ANSI escapes and control characters, then NFKC-normalizes;
  - truncates every string;
  - wraps tool output in `<untrusted-data source=…>`, adding a warning attribute when the text looks like instructions ("ignore previous instructions", fake `<system>` tags, Persian equivalents).
- **System prompt.** It tells the model never to follow instructions found in data, never to reveal secrets and never to approve anything.
- **The LLM has no shell.** It can only call allow-listed tools with schema-validated arguments: types, enums, patterns, ranges, and no extra keys.
- **Self-repair limits.** The LLM proposes file contents only. Paths are resolved inside the workspace (no `..`, no `.git`) and limited to safe extensions. Tests run with a fixed command in a sandbox: a throw-away copy, no secrets in the env, ulimits, a timeout, and no network where `unshare -rn` is permitted.
- **Abuse detector.** Each user message is classified as `ok`, `offtopic` or `attack` (regex plus a small LLM check). The score is sticky per login session; at the threshold the conversation is closed until the owner re-enters the password.
- **Uploads.** Zip uploads are capped at 60 MB and 6000 files, with no symlinks and no zip-slip. Git clones are https only, shallow, with symlinks disabled and a 90 s timeout. Nothing from an uploaded project is executed by the analyser.

## Infrastructure

- **Non-root container.** The image starts as root only to `chown` the mounted disk, then drops to uid 1000 with `setpriv`.
- **Least privilege does not exist on Liara's side.** Liara API tokens are team-wide; no per-app or scoped tokens exist. This is documented in [LIMITATIONS.md](LIMITATIONS.md). Negahban compensates with its own locks.
- **External watchdog.** It restarts Negahban only through the Liara API, with a cooldown, and logs every decision.
