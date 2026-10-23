# Control plane vs AI: why scaling doesn't need an LLM, and what the LLM is for

Negahban is split into two layers.

| | Deterministic control plane (no LLM) | AI layer (LLM, optional) |
|---|---|---|
| **What** | Monitoring loop, detectors, alert rules, autopilot (vertical resize up/down, restart), budget and cooldowns, approvals engine (fingerprints, one-use, locks), post-action verification, health/status page, daily report numbers, snapshots, watchdog | Chat and tool calling, plain-language explanations of incidents, novel hypotheses, code self-repair (patch writing), translation, report summary sentence, web search for unknown errors |
| **Inputs** | Liara metrics/events/logs/releases, own HTTP probes, owner policies and rules | The same evidence, fenced as untrusted data |
| **Decision** | Fixed rules with hysteresis, e.g. CPU ≥ 80 % for 5 min, inside the plan range and the daily toman cap, ≥ 10 min after the last resize | Proposes actions; every mutation is still an approval card (or an owner-pre-approved autopilot rule) |
| **Badge in UI** | ⚙️ قاعده‌محور | 🧠 با کمک AI (an incident shows ⚙️+🧠 when the LLM only explained a rule-based diagnosis) |

**Why the scaling decisions are rules:**
- They must be predictable, auditable, cheap and fast.
- They must keep working when the LLM provider is down, rate-limited or out of budget.
- They must never be "talked into" an action by text found in logs (prompt injection).

**What the AI adds:**
- It explains *why* to a non-technical owner.
- It ranks causes that need reading tracebacks or build output.
- It writes and tests code patches.
- It turns natural language into tool calls, every one of which goes through the same deterministic approval layer.

Horizontal scaling of demo-shop works the same way. It is decided by the platform's deterministic replica controller (`meow-lb`: min/max, cooldowns, budget, deploy quota, never removing the last healthy backend). Negahban's AI can only read its status (`scaler_status`) and *propose* an override (`scaler_override` approval card).

## "حالت بدون هوش مصنوعی" (AI off)

The switch is under «خودران و بودجه», or `PUT /api/control/ai {enabled:false}`. While AI is off:
- `llm.chat()` refuses *every* call before any network I/O. The counter `stats.blocked_ai_off` records how many calls were refused.
- Chat answers with a fixed notice.
- Monitoring, incidents, autopilot resize and restart, verification, alert rules, notifications, `/status` and the daily report (template text, no LLM summary) all keep running.
- The self-guardrail switches AI off automatically when the daily LLM spend cap is reached, and sends a notification.

Live proof (2026-10-23):
1. AI was switched off.
2. The traffic-spike scenario was run on demo-shop.
3. The orchestrator detected CPU saturation and the autopilot resized small → medium. Verification passed.
4. During this, the LLM call counter did not move.
5. AI was switched back on.

Evidence: `evidence/final-2026-10-23/aioff-before.json`, `aioff-after.json` and `live-aioff-traffic-spike.json`.

## Self-protection

| Mechanism | How |
|---|---|
| Supervised loops | `control.supervise()` wraps the orchestrator, job worker, discovery, resource self-check and snapshot loops. If one crashes, it logs `app.crash` (with the stack) and `loop.restart`, then restarts the loop with exponential backoff. The state of every loop is shown on the «گزارش عملیات» page. |
| Back-pressure | At most 3 chat turns run concurrently and at most 6 wait; further requests get HTTP 429. Each chat turn has a 300 s ceiling. The orchestrator never waits on the chat. |
| Load shedding | Every 20 s the container's cgroup memory is checked against its limit. At 85 % or more, chat and LLM features pause and monitoring continues. They resume below 75 %. At 95 % or more for 3 consecutive checks, `/healthz` returns 503, and the external watchdog restarts the container through the Liara API. |
| Durable state | All of these live in SQLite on the Liara disk: proposals (one-use, with fingerprints), incidents, jobs (an interrupted non-idempotent job goes to the dead-letter queue and can only be replayed as a new card), cooldown timestamps, policies, rules and memory. A restart resumes them. |
| Circuit breakers and retries | Each upstream has its own breaker (Liara API, each LLM provider). Retries use full-jitter backoff and respect `Retry-After`. |
| Self-guardrails | Daily LLM spend cap, total and per feature. When it is reached, AI switches off and a notification is sent. There is also a maximum number of automatic actions per hour. |

## Snapshots

- **Where and when:** `control.snapshot_loop()` writes a consistent online copy of the state DB (`sqlite3 .backup` API), compressed with gzip. The schedule is hourly; the first snapshot of each day is kept as a daily. Retention is 24 hourly plus 7 daily, in `/data/snapshots`. A manual snapshot can be taken from «گزارش عملیات».
- **Off-site copy:** the Hetzner `negahban-snapshot-pull.timer` runs daily at 03:15 UTC. It downloads `GET /api/snapshots/latest` (read-only, bearer `STATUS_INGEST_TOKEN`) into `/root/negahban-snapshots/`, keeps 14 copies and checks each with `gzip -t`. Log: `/var/log/negahban-snapshot-pull.log`.
- **Restore procedure** (tested 2026-10-23; evidence `evidence/final-2026-10-23/snapshot-restore-staging.txt`):
  1. `gunzip -c <snapshot>.db.gz > $DATA_DIR/negahban.db` (stop the app first, or use a fresh `DATA_DIR`).
  2. `sqlite3 $DATA_DIR/negahban.db 'pragma integrity_check'` must print `ok`.
  3. Start Negahban with that `DATA_DIR`. Incidents, proposals, audit, memory, jobs and policies are back.
  4. For production: upload the file to the Liara disk `data` with the file browser, or have the app copy it from `/data/snapshots`, then restart.

## External watchdog

`negahban-watchdog.timer` on Hetzner runs every 2 minutes. If `/healthz` fails twice, or the orchestrator lags by more than 5 minutes, it scales the app back on through the Liara API if it is off, otherwise restarts it. It waits at least 15 minutes between actions and logs to `/var/log/negahban-watchdog.log`. The revive path was triggered for real; evidence is in `evidence/final-2026-10-23/watchdog-*`.
