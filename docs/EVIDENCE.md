# Evidence matrix

Every judging criterion and every expected capability in the challenge brief, mapped to the feature that implements it and to live proof on Liara.

- `ev/` means `/opt/negahban/evidence/`.
- `acc/` means `ev/acceptance-2026-10-22/`.
- `fin/` means `ev/final-2026-10-23/`.
- Tests are in `tests/` (246+ passing).
- Live app: https://negahban-sre.liara.run. Status page: https://negahban-sre.liara.run/status.

## 1. Liara integration (100)

| Criterion / capability | Feature | Live proof | Tests |
|---|---|---|---|
| API connection | `negahban/liara.py`: an async client with team scoping, retries, a circuit breaker and redaction. It uses about 40 endpoints, listed in [LIARA_API.md](LIARA_API.md) | Every page of `acc/*.png` uses live Liara data | `test_liara_client.py` |
| Analyse project, recommend plan with real price, create app, configure, deploy | Initial-setup wizard «راه‌اندازی اولیه» plus `analyze_project` and `launch_project`. One card does create, deploy (build log streamed live), verify, keep the default subdomain and save a template | Live run on `github.com/render-examples/fastapi`: analysis in 2 s, card showing +1,042 toman/h, created and built, app served "Hello World". Evidence: `fin/setup-1-analysis.png` … `setup-4-result.png`, `setup-launch-proposal.json`, `setup-run.log`. Then deleted through the delete card (`setup-delete-proposal.json`). Verification timed out the first time because the new `*.liara.run` name needs about 3 minutes to route; verification now waits up to 6 minutes for new apps | `test_control_onboarding_reports.py::test_launch_card_…` |
| Domain and IRNIC guidance | Domain wizard (records + IRNIC steps + check + SSL); `set_default_subdomain` | `acc/desk-domains.png`; subdomain card in `acc/chat-subdomain.png` | `test_memory_grounding.py` (subdomain card) |
| GitOps: repos + CI/CD | `negahban/gitops.py` + `tools_gitops.py`: app→repo map (bazargard-ai→moazeniz/bazargard, negahban-sre→moazeniz/negahban, meowmeow-wallex→moazeniz/wallex-ai-trader); read tools `cicd_status` (CI per commit, check runs, last CI and Deploy runs, live commit vs main), `git_what_changed` (commit list + files), `deploy_incident_correlation`, `ci_run_details` (jobs/steps + failed log tail); sensitive card `release_via_pipeline` (workflow_dispatch of «Deploy to Liara», follows jobs/steps live, smoke test + auto-rollback inside the pipeline, direct upload named as fallback if GitHub is unreachable). Code-level allowlist: reads + deploy dispatch only | Dashboard «CI/CD (GitHub)» card and app-page section read live from GitHub (`gitops/desk-cicd-card.png`, `gitops/desk-cicd-app.png`, `gitops/mob-cicd-card.png`, `gitops/desk-cicd-changes.png`); release card for negahban-sre created and rejected, nothing dispatched (`gitops/desk-release-card.png`, `gitops/live-gitops.json`) | `test_gitops.py` (allowlist: 16 allowed / 27 blocked routes, blocked calls never reach the network; mocked GitHub for overview, what-changed, dispatch-and-follow, wallex refusal, unreachable fallback) |
| Managed databases + object storage | `negahban/diag/dbaas.py`, `storage.py`, `addons.py`: list/create/connect/power/resize/backup/restore/delete for databases and buckets/keys, every change a card with cost per hour and month plus the one-week balance check; typed name for destructive cards | Read-only live listing and price calls only; no real resources were created because credit is short | `test_diag_dbaas.py`, `test_diag_storage.py` |
| Publish new versions | `deploy_workspace`, `apply_code_fix` and `deploy_template`, all approval cards with the source archived | Redeploy via chat (`acc/chat-deploy.png`); template deploy READY with healthCheck (`ev/e2e-deploy_template-final-1792693697.json`) | `test_templates.py` |
| Manage config through Liara APIs | `set_env` / `unset_env` (full-list semantics, values masked), resize, scale, zero-downtime, default subdomain | set_env via chat verified (`acc/chat-set-env.png`); missing-env restore (`ev/live-acc-missing-env.json`) | `test_approvals_policy.py` |
| Successful infrastructure operations | Restart, resize up/down, rollback (Liara's console rollback endpoint), delete with typed name, IaaS virtual servers | `acc/results-ops.json` (5/5 approve → execute → verify); `ev/e2e-delete-app.json`; `acc/desk-servers-iaas.png` | `test_diag_iaas.py` |

## 2. Intelligence (100)

| Criterion / capability | Feature | Live proof | Tests |
|---|---|---|---|
| Log analysis | Paged v2 logs, traceback grouping, 5xx rate, slow requests, OOM/missing-env/restart-loop detection, structured-log explorer | `acc/log-explorer.png`; `ev/e2e-500-diagnosis.json` | `test_classifiers.py`, `test_diag_logintel.py` |
| Error detection | Orchestrator detectors every 60 s plus owner-editable alert rules (metric and log pattern) plus push ingest | `fin/alerts-proof.log`: push event fired in 0 s, traceback rule in 76 s, 5xx rule woke the agent | `test_alerts.py` |
| Root-cause ranking | Deterministic ranked hypotheses with confidence and evidence. The LLM explains and adds novel ones. Incident memory raises confidence for repeat problems | `ev/e2e-500-diagnosis.json` (ZeroDivisionError 90 %); `ev/e2e-build-failure-diagnosis.json` | `test_classifiers.py`, `test_memory_grounding.py::test_incident_memory_…` |
| Solution proposal | Fix tools attached to each hypothesis, as approval cards with cost, downtime and rollback | `acc/desk-approvals.png` | `test_approvals_policy.py` |
| Verifying the corrective action | Verification after every action: new container start, HTTP health, latency, no new 5xx or tracebacks from the new release, expected plan/env, memory | Every card in `ev/live-acc-*.json` ends "done" with checks; self-repair in `ev/live-e2e-live-500-selffix.json` | `test_selffix.py` |
| Self-repair (code) | Reproduce with tests → LLM patch → sandbox tests → diff card → deploy → verify | Bad release fixed live 5 times (e.g. `fin/alerts-proof.log`, `ev/live-acc-bad-release.json`) | `test_selffix.py` |

## 3. UX (50)

| Criterion / capability | Feature | Live proof |
|---|---|---|
| Simple interaction | Persian RTL dashboard (16 pages, mobile layout), chat with live step status, charts, diagrams and memory | `acc/desk-*.png`, `acc/mob-*.png`, `acc/chat-*.png` |
| First-run guidance | Skippable 6-step tour, remembered per owner; «راهنما» button | `fin/tour-1-desk.png`, `fin/tour-3-desk.png` |
| Understandable suggestions | Plain-Persian summaries, "why" on every card, daily report with recommendations | `fin/desk-reports.png`, `fin/daily-report.json` |
| Operation status display | Live ops feed, step list, build-log stream, verification checks on cards, 🔔 notifications | `fin/setup-3-building.png`, `fin/alerts-1-bell.png` |
| Approval management | One-use, expiring cards bound to the app state, rule-vs-AI badges, typed-name deletes, secrets never on cards | `acc/chat-restart-approved.png`, `acc/chat-locked.png` |

## 4. Reliability, maintenance and security (50)

| Criterion / capability | Feature | Live proof | Tests |
|---|---|---|---|
| Health and resource monitoring | 60 s probe + metrics + events, second vantage point for `/status`, daily report | `/status` page; `fin/daily-report.json` | `test_api_mcp.py::test_status_vantage_and_page` |
| Automation of repetitive ops | Autopilot within a budget: vertical scale up and down, restart on leak, alert rules, scheduled report, snapshots | Scale up and down (`ev/live-acc-traffic-spike.json`), leak restart (`acc/scenarios-run.log`) | `test_orchestrator_merge.py`, `test_alerts.py` |
| Horizontal scaling + load balancing (deterministic, no LLM; Negahban's AI as advisor) | meow-lb (https://meow-lb.liara.run), part of the platform layer: least-connections load balancer with active health checks, passive outlier ejection and retry for idempotent requests. A rule-based replica controller (`scaler/meowlb/policy.py`) with sustain windows, hysteresis, cooldowns, min/max, daily budget, deploy quota, the one-week balance rule and never removing the last healthy backend. Negahban reads it with `scaler_status` and proposes overrides with `scaler_override` approval cards | 15 rps for 12 min: the guard held on the one-week rule → demo-shop-r1 created and healthy in 55 s → traffic split 75/75 per 10 s → the base app was briefly ejected during a resize and r1 served 100 % → load stopped → r1 drained and deleted after 120 s of calm. One scale-out succeeded, one was refused by Liara's 429 (one-week rule), and there was one scale-in. A separate CPU-triggered scale-out and scale-in ran at 06:51–06:59 (`ev/scaling-20261023T065958Z/`, `ev/SCALING_RUN.md`, `docs/SCALING.md`) | `scaler/tests` (25) |
| Works without AI (deterministic control plane) | AI-off mode; badges | **AI off → traffic spike → autopilot resize small→medium verified with 0 LLM calls** (`fin/aioff-before.json` / `aioff-after.json`: LLM calls 18 → 18) | `test_control_onboarding_reports.py::test_ai_off_…` |
| Secure handling of sensitive actions | Approval engine, write/env locks, redaction, CSRF, rate limits, prompt-injection fencing, abuse detector, no shell for the LLM, sandboxed tests | `acc/chat-locked.png`; [SECURITY.md](SECURITY.md) | `test_sanitize_redact.py`, `test_agent_guard.py`, `test_api_mcp.py` |
| Self-protection | Supervised loops, back-pressure, load shedding, durable jobs with dead-letter, circuit breakers, LLM spend caps → AI off | Audit page «سلامت خود نگهبان» (`fin/desk-audit.png`) | `test_resilience_jobs.py`, `test_alerts.py::test_llm_budget_…` |
| External watchdog | Hetzner timer → Liara API | **Outage simulated at 07:04:09 (scale 0) → watchdog acted at 07:07:56 → healthy at 07:08:16** (`fin/watchdog-revive.log`) | — |
| Backups | Hourly + daily SQLite snapshots, off-site pull timer, tested restore | `fin/snapshot-pull.log`, `fin/snapshot-restore-staging.txt` (integrity ok, all tables restored) | `test_control_onboarding_reports.py::test_snapshot_…` |
| Declaring API limitations | «محدودیت‌های API» page plus [LIMITATIONS.md](LIMITATIONS.md) | `acc/desk-limits.png` | — |
| LLM cost control | Token/cost logging per call, per-day usage card, prompt caching | **Caching: input cost −53.8 % on identical chats; 58k tokens served from cache** (`fin/prompt-cache-measurement.json`) | `test_llm_providers.py::test_usage_is_logged_and_persisted` |
| Notifications | Bell, browser notification, escalation, ack/resolve; Liara mail / Bale / webhook adapters, disabled until configured | `fin/alerts-1-bell.png`, `fin/alerts-2-page.png`, `fin/alerts-3-acked.png`, `fin/alerts-proof.json` (browser notification captured) | `test_alerts.py` |

## 5. Deliverables

| Deliverable | Where |
|---|---|
| Runnable MVP (request → operation/diagnosis) | https://negahban-sre.liara.run; demo app https://demo-shop.liara.run |
| Markdown documentation | [README.md](../README.md), [ARCHITECTURE.md](ARCHITECTURE.md), [CONTROL_PLANE.md](CONTROL_PLANE.md), [LIARA_API.md](LIARA_API.md), [ALERTS.md](ALERTS.md), [SECURITY.md](SECURITY.md), [LIMITATIONS.md](LIMITATIONS.md), [USER_GUIDE.md](USER_GUIDE.md), [DIAGNOSTICS.md](DIAGNOSTICS.md) |
| Demo video script | [DEMO_VIDEO_SCRIPT.fa.md](DEMO_VIDEO_SCRIPT.fa.md) (verified clicks and timings), [DEMO_SCRIPT_FA.md](DEMO_SCRIPT_FA.md) |
