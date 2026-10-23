#!/usr/bin/env bash
# Live scaling demo with evidence capture (bounded): load -> scale out -> load stops -> scale in.
#   scaling_demo.sh [RPS] [LOAD_MINUTES] [WATCH_MINUTES]
set -u
RPS=${1:-15}; LOAD_MIN=${2:-14}; WATCH_MIN=${3:-35}
LB=${LB_URL:-https://meow-lb.liara.run}
OUT=/opt/negahban/evidence/scaling-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"; cd "$(dirname "$0")"
PY=${PY:-/opt/ops/venv/bin/python}
HOST=${LB#https://}; IP=$(dig +short "$HOST" @1.1.1.1 | tail -n 1)   # the host resolver may hold a stale NXDOMAIN
RES=(--resolve "$HOST:443:$IP")
echo "{\"started\":\"$(date -u +%FT%TZ)\",\"rps\":$RPS,\"load_minutes\":$LOAD_MIN,\"lb\":\"$LB\"}" > "$OUT/run.json"
( end=$(( $(date +%s) + WATCH_MIN*60 ))
  while [ "$(date +%s)" -lt "$end" ]; do
    s=$(curl -s -m 15 "${RES[@]}" "$LB/scaler/status"); l=$(curl -s -m 15 "${RES[@]}" "$LB/lb/status")
    printf '{"t":"%s","scaler":%s,"lb":%s}\n' "$(date -u +%FT%TZ)" "${s:-null}" "${l:-null}" >> "$OUT/status.jsonl"
    sleep 15
  done ) &
WATCH=$!
$PY loadtest.py "$LB" --rps "$RPS" --minutes "$LOAD_MIN" --path / --resolve "$HOST:$IP" --out "$OUT/loadtest.jsonl" > /dev/null
echo "load finished $(date -u +%FT%TZ)" >> "$OUT/run.json.log"
wait $WATCH
curl -s -m 15 "${RES[@]}" "$LB/scaler/audit?limit=200" > "$OUT/audit.json"
curl -s -m 15 "${RES[@]}" "$LB/lb/status" > "$OUT/lb-final.json"
echo "done $OUT"
