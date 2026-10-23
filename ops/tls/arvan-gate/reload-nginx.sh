#!/bin/sh
# certbot deploy hook: only reload when the full config still validates.
set -eu
/usr/sbin/nginx -t >/dev/null 2>&1
/usr/bin/systemctl reload nginx.service
