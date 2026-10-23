"""Secret redaction for anything that leaves the process: logs, UI payloads, LLM prompts, audit rows."""
import re
from .config import settings

_PATTERNS = [
    (re.compile(r'(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}'), r'\1 ***'),
    (re.compile(r'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}'), '***jwt***'),
    (re.compile(r'\bsk-[A-Za-z0-9_\-]{12,}'), 'sk-***'),
    (re.compile(r'(?i)\b(password|passwd|secret|token|api[_-]?key|private[_-]?key)(["\']?\s*[:=]\s*["\']?)([^\s"\',;&]{4,})'), r'\1\2***'),
    (re.compile(r'(?i)(postgres|mysql|mongodb(?:\+srv)?|redis|amqp)://([^:/\s]+):([^@/\s]+)@'), r'\1://\2:***@'),
]
SENSITIVE_ENV = re.compile(r'(?i)(token|secret|password|passwd|key|credential|private|dsn|database_url|_url$|auth)')


def redact(text, extra=()):
    if text is None:
        return text
    s = str(text)
    for sec in [*settings.secrets(), *extra]:
        if sec and len(sec) >= 6:
            s = s.replace(sec, '***')
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    return s


def redact_obj(o):
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, dict):
        return {k: ('***' if isinstance(v, str) and SENSITIVE_ENV.search(str(k)) and k not in ('key', 'url', 'publicUrl') and len(v) > 3 else redact_obj(v)) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [redact_obj(x) for x in o]
    return o


def mask_envs(envs):
    """Liara returns env values in clear text; the UI and the LLM only ever see masked values for sensitive keys."""
    out = []
    for e in envs or []:
        k, v = e.get('key', ''), str(e.get('value', ''))
        sens = bool(SENSITIVE_ENV.search(k))
        out.append({'key': k, 'value': ('•••' + v[-2:] if len(v) > 6 else '•••') if sens else redact(v), 'masked': sens})
    return out
