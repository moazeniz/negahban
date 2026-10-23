#!/bin/sh
# Temporary demo switches of a meow-lb replica controller. Both revert by themselves when their time is up.
#   demo_mode.sh on   [lab|prod] [MINUTES]   short demo timings (sustain 45 s out / 60 s in, cooldowns 60 s); default 90 min
#   demo_mode.sh off  [lab|prod]             normal policy now
#   demo_mode.sh dry  [lab|prod] [MINUTES]   dry run: decisions are audited, nothing is created or resized; default 15 min
#   demo_mode.sh dryoff [lab|prod]
#   demo_mode.sh status [lab|prod]           modes, effective timings, last decision, last 6 audit entries
# The admin token is read from /root/.meow-scaler.env and never printed.
set -eu
cmd=${1:-status}; where=${2:-lab}; minutes=${3:-}
case "$where" in
  lab) url=https://meow-lb-lab.liara.run ;;
  prod) url=https://meow-lb.liara.run ;;
  *) echo "where must be lab or prod" >&2; exit 2 ;;
esac
tok=$(sed -n 's/^SCALER_ADMIN_TOKEN=//p' /root/.meow-scaler.env | tr -d "\"'")
post() { curl -fsS -m 20 -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d "$1" "$url/scaler/override"; echo; }
case "$cmd" in
  on) post "{\"action\":\"demo_mode\",\"minutes\":${minutes:-90},\"reason\":\"video recording\",\"actor\":\"human\"}" ;;
  off) post '{"action":"demo_off","reason":"recording finished","actor":"human"}' ;;
  dry) post "{\"action\":\"dry_run\",\"minutes\":${minutes:-15},\"reason\":\"rehearsal\",\"actor\":\"human\"}" ;;
  dryoff) post '{"action":"dry_off","reason":"rehearsal finished","actor":"human"}' ;;
  status) curl -fsS -m 20 "$url/scaler/status" | python3 -c '
import json, sys, time
d = json.load(sys.stdin); p = d["policy"]; m = d.get("modes") or {}
left = lambda t: f"{int((t - time.time()) / 60)} min left" if t else "off"
print("team:", d.get("team"), "| app:", d["app"], "| plan:", d.get("base_plan"), "| instances:", d["instances"], d["replicas"])
print("demo mode:", left(m.get("demo_until")), "| dry run:", left(m.get("dry_run_until")))
print("timings: out sustain", p["scale_out"]["sustain_s"], "s, in sustain", p["scale_in"]["sustain_s"], "s, cooldowns",
      p["cooldown_out_s"], "/", p["cooldown_in_s"], "s")
ld = d.get("last_decision") or {}
print("last decision:", ld.get("action"), "-", ld.get("reason_fa") or ld.get("reason"))
for a in d.get("audit_tail", [])[-6:]:
    print(" ", time.strftime("%H:%M:%S", time.gmtime(a["ts"])), a["kind"], a["result"], "-", a.get("reason_fa") or a.get("reason", ""))
' ;;
  *) echo "usage: demo_mode.sh on|off|dry|dryoff|status [lab|prod] [MINUTES]" >&2; exit 2 ;;
esac
