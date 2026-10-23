#!/bin/sh
# Off-site copy of Negahban state snapshots (pulled from Liara to this host). Keeps the newest 14.
set -e
. /root/.negahban-admin.env
D=/root/negahban-snapshots; mkdir -p $D; chmod 700 $D
F=$D/negahban-$(date -u +%Y%m%dT%H%M%SZ).db.gz
curl -sf -m 120 -H "Authorization: Bearer $STATUS_INGEST_TOKEN" -o $F https://negahban-sre.liara.run/api/snapshots/latest
gzip -t $F && echo "$(date -u +%FT%TZ) pulled $(basename $F) $(stat -c %s $F) bytes" >> /var/log/negahban-snapshot-pull.log
ls -1t $D/negahban-*.db.gz | tail -n +15 | xargs -r rm -f
