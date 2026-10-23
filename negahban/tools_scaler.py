"""Horizontal scaling integration with the platform's deterministic replica controller + load balancer (meow-lb).
Negahban reads its public status (backends, decisions, policy) and can request an override ONLY through an owner-approved
card; the controller itself enforces min/max, budget, deploy quota, cooldown and never-remove-last."""
import asyncio
import os
import httpx
from .tools import ToolError, tool

BASE = lambda: (os.environ.get('SCALER_URL') or os.environ.get('LB_URL') or 'https://meow-lb.liara.run').rstrip('/')
LAB = lambda: (os.environ.get('SCALER_LAB_URL') or 'https://meow-lb-lab.liara.run').rstrip('/')  # test-team lab stack (temporary)


async def _get(path, token=None, base=None, timeout=15):
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get((base or BASE()) + path, headers={'Authorization': f'Bearer {token}'} if token else {})
    if r.status_code >= 400:
        raise ToolError(f'scaler {path}: HTTP {r.status_code}')
    return r.json()


async def status():
    out = {'url': BASE()}
    for key, path in (('scaler', '/scaler/status'), ('lb', '/lb/status')):
        try:
            out[key] = await _get(path)
        except Exception as e:
            out[key] = {'error': str(e)[:160]}
    tok = os.environ.get('LB_READ_TOKEN')
    if tok:
        try:
            out['replicas'] = await _get('/api/replicas', tok)
        except Exception as e:
            out['replicas'] = {'error': str(e)[:160]}
    try:  # the lab load balancer in the test team, shown only while it exists (public read-only endpoints)
        sc, lb = await asyncio.gather(_get('/scaler/status', base=LAB(), timeout=6), _get('/lb/status', base=LAB(), timeout=6))
        out['lab'] = {'url': LAB(), 'scaler': sc, 'lb': lb}
    except Exception:
        pass
    return out


@tool('scaler_status', 'read', 'Horizontal scaling status from the load balancer / replica controller (meow-lb): backends (health, p95, in-flight), '
      'per-app mode (replicas vs single-instance with reason), policy bounds, last decision and its reason, cost today vs budget.', {}, [],
      label_fa='در حال خواندن وضعیت مقیاس افقی…')
async def scaler_status(a, ctx):
    return await status()


async def _override_impact(a):
    return ({'downtime': 'ندارد (بالانس‌کننده ترافیک را جابه‌جا می‌کند)', 'cost': 'هر رپلیکای اضافه هزینه‌ی یک برنامه‌ی small-g2 را دارد',
             'risk': 'کم — کنترلر حد پایین/بالا، بودجه، سهمیه و «حذف نکردن آخرین نمونه‌ی سالم» را خودش اعمال می‌کند',
             'command': f"{a.get('action')} {a.get('app', 'demo-shop')} min={a.get('min')} max={a.get('max')} minutes={a.get('minutes')}"},
            'دستور resume یا hold با همین ابزار؛ سیاست خودکار پس از پایان مدت دوباره فعال می‌شود.')


@tool('scaler_override', 'sensitive', 'Ask the replica controller to scale_out / scale_in / hold / resume / set_bounds for demo-shop (horizontal). '
      'Creates an approval card; the controller still enforces its policy.',
      {'action': {'type': 'string', 'enum': ['scale_out', 'scale_in', 'hold', 'resume', 'set_bounds']}, 'app': {'type': 'string'},
       'min': {'type': 'integer', 'minimum': 1, 'maximum': 5}, 'max': {'type': 'integer', 'minimum': 1, 'maximum': 5},
       'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 240}, 'reason': {'type': 'string'}}, ['action'],
      title=lambda a: f"مقیاس افقی: {a.get('action')} برای {a.get('app') or 'demo-shop'}", impact=_override_impact, label_fa='ساخت کارت مقیاس افقی…')
async def scaler_override(a, ctx):
    tok = os.environ.get('SCALER_ADMIN_TOKEN') or os.environ.get('LB_WRITE_TOKEN')
    if not tok:
        raise ToolError('scaler admin token is not configured on Negahban')
    body = {k: a[k] for k in ('action', 'min', 'max', 'minutes') if a.get(k) is not None}
    body.update(reason=(a.get('reason') or 'owner-approved via Negahban')[:200], actor='negahban-ai' if ctx.get('origin') == 'chat' else 'human')
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(BASE() + '/scaler/override', json=body, headers={'Authorization': f'Bearer {tok}'})
    if r.status_code >= 400:
        raise ToolError(f'controller refused: HTTP {r.status_code} {r.text[:200]}')
    return r.json()
