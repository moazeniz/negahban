# Guardrail rules, push events and notifications

All of this is deterministic. The LLM is only woken (action `wake`) to diagnose and explain.

## Rules (page «قوانین هشدار»)

| Field | Meaning |
|---|---|
| `kind` | `metric` or `log` |
| `metric` | `http_5xx_rate` (%), `latency_p95` (ms), `cpu_pct` / `mem_pct` (% of the plan limit), `restarts`, `oom`, `down`, `deploy_failed`, `credit_days`, `burn_rate` (toman/h), `llm_spend_day` (USD) |
| `op`, `threshold` | `>` or `<` |
| `window_min`, `min_duration_min` | Evaluation window, and how long the condition must hold before firing |
| `pattern` (log) | A keyword, `re:<regex>`, or structured fields such as `event=app.crash level=critical` (matched against JSON log lines and pushed events) |
| `scope` | `*`, an app name, or `_team` for credit, burn and LLM spend |
| `severity` | `info`, `warning` or `critical` |
| `action` | `notify`, or `wake` (opens an incident and queues automatic diagnosis), or `autopilot:restart_app`. The autopilot action goes through the approvals engine, policy and locks |
| `cooldown_min`, `quiet_hours` (e.g. `23-7`, Tehran time), `dedup` | Spam control. Critical alerts ignore quiet hours |

Rules are versioned (`/api/alerts/rules/{id}/versions`) and audited. A set of sensible defaults is created on the first start. `POST /api/alerts/test-rule` dry-runs a log pattern against sample lines.

## Push trigger (near real time)

```
POST https://negahban-sre.liara.run/api/ingest/event
Authorization: Bearer <per-app token from «قوانین هشدار» → توکن>
Content-Type: application/json

{"event":"app.crash","level":"critical","app":"bazargard-ai","msg":"...","error_type":"KeyError"}
```

- The body can also be a list of events (up to 50).
- The rate limit is 120 requests per minute per app.
- Log rules are evaluated immediately, so the app doesn't have to wait for the 60 s poll.

Reference sender (stdlib only; never let it raise):

```python
import json, os, threading, urllib.request

def negahban_push(event, level='error', **fields):
    tok = os.environ.get('NEGAHBAN_INGEST_TOKEN')
    if not tok:
        return
    body = json.dumps({'event': event, 'level': level, **fields}).encode()
    req = urllib.request.Request('https://negahban-sre.liara.run/api/ingest/event', data=body, method='POST',
                                 headers={'Authorization': f'Bearer {tok}', 'Content-Type': 'application/json'})
    threading.Thread(target=lambda: urllib.request.urlopen(req, timeout=5), daemon=True).start()

# e.g. in a global exception handler / dead-letter hook:
# negahban_push('app.crash', 'critical', error_type=type(e).__name__, msg=str(e)[:200])
```

## Notifications

- **In the dashboard:**
  - 🔔 bell with the count of open notifications;
  - toast on every new notification;
  - browser notification after opt-in on «قوانین هشدار».
- **Escalation:** an unacknowledged notification is re-sent at 10 min (severity raised to critical) and at 30 min.
- **Owner actions:** «دیدم» (acknowledge) and «برطرف شد» (resolve). Links go to the incident or approval card.
- **Channels** (disabled until configured; each has a «آزمایش» button):
  - **Email** via Liara's mail service (`POST /api/v1/mails/{id}/messages`). It needs a mail plan. The only available plan, `m2`, costs **1,475,000 toman** (per month) and needs a domain. **Not purchased.**
  - **Bale bot:** `https://tapi.bale.ai/bot<token>/sendMessage`. Set the token and chat id.
  - **Generic webhook:** POST JSON.

## Self-guardrails

- **Daily LLM spend cap:** a total cap (default $2) plus per-feature caps (chat, self-repair, diagnosis). When the cap is reached, Negahban switches to the AI-off mode and sends a notification. Monitoring and the autopilot keep running.
- **Automatic-action cap:** at most N automatic actions per hour (default 6). After that, cards wait for the owner.
