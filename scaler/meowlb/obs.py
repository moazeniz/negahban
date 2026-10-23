"""Structured JSON logs (team standard): one object per line on stdout, secrets redacted."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

APP = "meow-lb"
RELEASE = "1.1.0"
_SECRET = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def redact(text: str) -> str:
    s = str(text)
    for name in ("LIARA_API_TOKEN", "SCALER_ADMIN_TOKEN", "SCALER_READ_TOKEN"):
        v = os.getenv(name)
        if v and len(v) >= 8:
            s = s.replace(v, "***")
    return _JWT.sub("***", _SECRET.sub(lambda m: m.group(1) + "***", s))


def log(event: str, level: str = "info", msg: str = "", **fields) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "level": level,
           "app": APP, "env": os.getenv("OBS_ENV", "dev"), "release": RELEASE, "event": event,
           "request_id": fields.pop("request_id", None), "trace_id": fields.pop("trace_id", None),
           "span_id": fields.pop("span_id", None), "session": None, "msg": msg[:200]}
    for k, v in fields.items():
        if isinstance(v, str):
            v = redact(v)[:500]
        rec[k] = v
    sys.stdout.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
