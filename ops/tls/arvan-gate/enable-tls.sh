#!/usr/bin/env bash
# HTTPS for the MeowMeow LLM gate on the Arvan host, with a Let's Encrypt IP-address certificate.
# Run on the Arvan host from this directory, as a sudo-capable user, with GATE_IP=<public IPv4 of the host>.
#
#   ./enable-tls.sh preflight     read-only: tools, ACME/PyPI reachability, port 8443 free, current gate block
#   ./enable-tls.sh stage-test    copy /etc/nginx to a temp dir, apply every edit there, `nginx -t` the copy
#   ./enable-tls.sh apply         backup -> edits -> nginx -t -> reload -> certificate -> TLS server -> verify
#   ./enable-tls.sh verify        compare every public route's status code with the pre-change snapshot
#   ./enable-tls.sh rollback FILE restore /etc/nginx from a backup tarball, nginx -t, reload
#
# The plain-HTTP gate (http://<ip>/meow-llm/...) keeps working; it is removed only after every Liara app
# uses https://<ip>:8443/meow-llm (separate, coordinated change).
set -euo pipefail
cd "$(dirname "$0")"
IP=${GATE_IP:?set GATE_IP to the gate host public IPv4, e.g. GATE_IP=203.0.113.10 $0 preflight}
PORT=${GATE_TLS_PORT:-8443}
SITE=/etc/nginx/sites-enabled/onboarding.conf
STATE=/var/lib/meow-tls
CERTBOT=${CERTBOT:-/opt/certbot/bin/certbot}
# Public routes on this host that must answer exactly as before (path -> status recorded by `snapshot`).
ROUTES=(/ /hamrah/ /sms-relay/ /pay /ig-feed/feed.json /api/ /liara-data/catalog.db.gz /meow-llm/health /meow-llm/nope)

say() { printf '\n== %s\n' "$*"; }
code() { curl -s -o /dev/null -m 20 -w '%{http_code}' "$@" || true; }

snapshot() {
  sudo mkdir -p "$STATE"
  for r in "${ROUTES[@]}"; do printf '%s %s\n' "$r" "$(code "http://127.0.0.1$r" -H "Host: $IP")"; done | sudo tee "$STATE/routes.before" >/dev/null
  cat "$STATE/routes.before"
}

verify() {
  local bad=0
  say "route status codes vs. snapshot ($STATE/routes.before)"
  while read -r r want; do
    got=$(code "http://127.0.0.1$r" -H "Host: $IP")
    if [ "$got" = "$want" ]; then echo "ok   $r $got"; else echo "DIFF $r was $want now $got"; bad=1; fi
  done < "$STATE/routes.before"
  if sudo test -e "/etc/letsencrypt/live/$IP/fullchain.pem" && sudo test -e /etc/nginx/sites-enabled/meow-llm-tls.conf; then
    say "TLS front door https://$IP:$PORT"
    got=$(code "https://$IP:$PORT/meow-llm/health")         # no token -> 401 proves routing + a trusted chain
    echo "https gate without token: $got (want 401)"; [ "$got" = 401 ] || bad=1
    echo | openssl s_client -connect "$IP:$PORT" -servername "$IP" 2>/dev/null | openssl x509 -noout -issuer -enddate -ext subjectAltName || bad=1
  fi
  return $bad
}

# Rewrites the :80 server so the gate locations come from the shared snippet and ACME challenges are served.
# Refuses to touch the file unless the current gate block is exactly the expected one.
edit_site() {  # $1 = site file to edit in place
  python3 - "$1" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
start = s.index("    # MeowMeow LLM gate")
end = s.index("location /meow-llm/ { return 404; }", start) + len("location /meow-llm/ { return 404; }")
block = s[start:end]
for must in ("location ~ ^/meow-llm/(v1/messages|v1/models|health|routing|place-context)$",
             "meow-llm.disabled", "$meow_llm_window", "$meow_llm_auth", "proxy_pass http://127.0.0.1:9894;",
             "include /etc/nginx/meow-llm-upstream.conf;"):
    if must not in block:
        sys.exit(f"unexpected gate block (missing {must!r}); not editing")
new = ("    # MeowMeow LLM gate: locations in snippets/meow-llm-locations.conf (shared with the :8443 TLS server).\n"
       "    include /etc/nginx/snippets/meow-llm-locations.conf;\n"
       "    include /etc/nginx/snippets/acme-challenge.conf;")
open(p, "w").write(s[:start] + new + s[end:])
print(f"edited {p}: gate block -> include (removed {block.count(chr(10)) + 1} lines)")
PY
}

case "${1:-}" in
preflight)
  say "tools"; nginx -v 2>&1; python3 --version; (certbot --version 2>&1 || true); ($CERTBOT --version 2>&1 || echo "no $CERTBOT yet")
  say "reachability"
  for u in https://acme-v02.api.letsencrypt.org/directory https://pypi.org/simple/certbot/; do echo "$u -> $(code "$u")"; done
  say "port $PORT"; if sudo ss -ltn "( sport = :$PORT )" | grep -q LISTEN; then echo "IN USE"; exit 1; else echo free; fi
  say "current gate block"; sudo grep -n -A13 "# MeowMeow LLM gate" "$SITE" | sed -E 's/[A-Za-z0-9_-]{24,}/<redacted>/g'
  say "route snapshot (not saved)"; for r in "${ROUTES[@]}"; do echo "$r $(code "http://127.0.0.1$r" -H "Host: $IP")"; done ;;

stage-test)
  T=$(sudo mktemp -d /root/nginx-stage.XXXXXX)
  trap 'sudo rm -rf "$T"' EXIT
  sudo cp -a /etc/nginx/. "$T/"
  sudo install -d "$T/snippets"
  sudo cp meow-llm-locations.conf acme-challenge.conf "$T/snippets/"
  sed "s/__GATE_IP__/$IP/g" meow-llm-tls.conf | sudo tee "$T/sites-enabled/meow-llm-tls.conf" >/dev/null
  sudo bash -c "$(declare -f edit_site); edit_site '$T/sites-enabled/onboarding.conf'"
  # Point the copy at itself; the LE certificate does not exist yet, so use a throwaway self-signed one.
  sudo openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=$IP" -keyout "$T/stage.key" -out "$T/stage.crt" 2>/dev/null
  sudo grep -rl '/etc/nginx' "$T" | xargs sudo sed -i "s#/etc/nginx#$T#g"
  sudo sed -i "s#/etc/letsencrypt/live/$IP/fullchain.pem#$T/stage.crt#; s#/etc/letsencrypt/live/$IP/privkey.pem#$T/stage.key#" "$T/sites-enabled/meow-llm-tls.conf"
  say "nginx -t on the staged copy"
  sudo nginx -t -c "$T/nginx.conf" 2>&1 | grep -v "conflicting server name"
  say "staged gate server"; sudo grep -n -E "listen|include|ssl_certificate " "$T/sites-enabled/meow-llm-tls.conf"
  say "staged :80 gate lines"; sudo grep -n -E "meow-llm|acme" "$T/sites-enabled/onboarding.conf" ;;

apply)
  sudo mkdir -p "$STATE"
  B="/root/nginx-backup-$(date -u +%Y%m%dT%H%M%SZ).tgz"
  say "1/7 backup -> $B"; sudo tar czf "$B" -C / etc/nginx; sudo chmod 600 "$B"
  say "2/7 route snapshot"; snapshot
  say "3/7 certbot >= 5 (IP-address certificates)"
  if ! $CERTBOT --version 2>/dev/null | grep -qE 'certbot ([5-9]|[1-9][0-9])\.'; then
    sudo python3 -m venv /opt/certbot && sudo /opt/certbot/bin/pip install -q 'certbot>=5.1,<6'
  fi
  $CERTBOT --version
  say "4/7 shared gate snippet + ACME location"
  sudo install -d -m 755 /etc/nginx/snippets /var/www/letsencrypt
  sudo install -m 644 meow-llm-locations.conf acme-challenge.conf /etc/nginx/snippets/
  sudo bash -c "$(declare -f edit_site); edit_site '$SITE'"
  if ! sudo nginx -t 2>&1 | grep -q "test is successful"; then
    echo "nginx -t failed; restoring $B"; sudo tar xzf "$B" -C /; sudo nginx -t && exit 1
  fi
  sudo systemctl reload nginx; sleep 1
  verify || { echo "routes changed; restoring $B"; sudo tar xzf "$B" -C /; sudo nginx -t && sudo systemctl reload nginx; exit 1; }
  say "5/7 certificate (shortlived profile, webroot)"
  sudo install -d /etc/letsencrypt/renewal-hooks/deploy
  sudo install -m 755 reload-nginx.sh /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  sudo $CERTBOT certonly --non-interactive --agree-tos --register-unsafely-without-email \
       --webroot -w /var/www/letsencrypt --preferred-profile shortlived --ip-address "$IP"
  say "6/7 TLS server on :$PORT"
  sed "s/__GATE_IP__/$IP/g" meow-llm-tls.conf | sudo tee /etc/nginx/sites-available/meow-llm-tls.conf >/dev/null
  sudo ln -sf /etc/nginx/sites-available/meow-llm-tls.conf /etc/nginx/sites-enabled/meow-llm-tls.conf
  sudo nginx -t && sudo systemctl reload nginx
  say "7/7 renewal timer + verification"
  # certbot from pip has no timer of its own: renew twice a day (short-lived certs live ~6 days).
  printf '[Unit]\nDescription=certbot renew (meow-llm gate)\n[Service]\nType=oneshot\nExecStart=%s renew -q\n' "$CERTBOT" | sudo tee /etc/systemd/system/meow-certbot.service >/dev/null
  printf '[Unit]\nDescription=certbot renew twice a day\n[Timer]\nOnCalendar=*-*-* 00,12:17:00\nRandomizedDelaySec=30m\nPersistent=true\n[Install]\nWantedBy=timers.target\n' | sudo tee /etc/systemd/system/meow-certbot.timer >/dev/null
  sudo systemctl daemon-reload && sudo systemctl enable --now meow-certbot.timer
  verify && echo "DONE. Backup: $B. Switch apps with LLM_GATE_URL=https://$IP:$PORT/meow-llm" ;;

verify) verify ;;

rollback)
  B=${2:?usage: rollback /root/nginx-backup-....tgz}
  sudo rm -f /etc/nginx/sites-enabled/meow-llm-tls.conf
  sudo tar xzf "$B" -C /
  sudo nginx -t && sudo systemctl reload nginx && echo "restored $B" && verify ;;

*) sed -n '2,12p' "$0"; exit 2 ;;
esac
