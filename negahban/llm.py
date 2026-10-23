"""LLM layer with two providers and automatic fail-over.

* anthropic (default): Anthropic Messages API via the team's Arvan LLM gate (LLM_GATE_URL + LLM_GATE_TOKEN; the gate attaches
  the real credential, enforces a time window (403 after it), a kill switch (503) and a rate limit). Model claude-sonnet-5-5,
  non-streaming, max_tokens <= 5000, no temperature; thinking is switched off for the tool loop
  (claude-haiku-5-5 default; gpt-6-luna maps to haiku, gpt-6.1-sol to sonnet).
* boxapi: OpenAI-compatible chat/completions with 3 keys in rotation (round-robin + per-key cooldown on 401/402/403/429/5xx).
  Quirks: `max_completion_tokens`; function tools need reasoning_effort='none' on gpt-6-luna; no temperature.

The agent speaks the OpenAI message format internally; the Anthropic path converts both ways and keeps the raw assistant
content blocks so a tool loop is replayed byte-for-byte (append-only history). If the active provider is unavailable
(401/402/403/429/5xx/timeout/credit) the call is retried once on the other provider. Keys are never logged."""
import asyncio, contextvars, json, logging, os, time
import httpx
from .config import settings

log = logging.getLogger('negahban.llm')
GATE_URL = os.environ.get('LLM_GATE_URL', '').rstrip('/')
GATE_TOKEN = os.environ.get('LLM_GATE_TOKEN', '')
ANTHROPIC_MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-haiku-5-5')  # gpt-6-luna tier <-> claude-haiku-5-5
GATE_WINDOW_END = os.environ.get('LLM_GATE_WINDOW_END', '2026-10-25T16:00:00Z')
feature = contextvars.ContextVar('llm_feature', default='other')  # chat | diagnose | selffix | guard | translate | report | ...
# USD per 1M tokens (in, out, cache_read, cache_write) - Anthropic list prices; BoxAPI returns its own cost
PRICES = {'claude-haiku-5-5': (0.10, 0.50, 0.01, 0.125), 'claude-sonnet-5-5': (2.0, 10.0, 0.20, 2.5)}
USAGE_SCHEMA = """create table if not exists llm_usage(day text, provider text, model text, feature text, calls int default 0,
  tokens_in int default 0, tokens_out int default 0, cache_read int default 0, cache_write int default 0, cost_usd real default 0,
  primary key(day, provider, model, feature));"""


def prompt_cache_on():
    try:
        from . import store
        v = store.kv_get('prompt_cache')
    except Exception:
        v = None
    return (os.environ.get('PROMPT_CACHE', '1') == '1') if v is None else bool(v)


def record_usage(provider, model, u):
    """Persist per-day usage (UTC day) per provider/model/feature."""
    try:
        from . import store
        from contextlib import closing
        with store._lock, closing(store._db()) as c:
            c.executescript(USAGE_SCHEMA)
            c.execute("""insert into llm_usage(day,provider,model,feature,calls,tokens_in,tokens_out,cache_read,cache_write,cost_usd) values(?,?,?,?,1,?,?,?,?,?)
                on conflict(day,provider,model,feature) do update set calls=calls+1, tokens_in=tokens_in+excluded.tokens_in, tokens_out=tokens_out+excluded.tokens_out,
                cache_read=cache_read+excluded.cache_read, cache_write=cache_write+excluded.cache_write, cost_usd=cost_usd+excluded.cost_usd""",
                      (time.strftime('%Y-%m-%d', time.gmtime()), provider, model, feature.get(), u['tokens_in'], u['tokens_out'], u['cache_read'], u['cache_write'], u['cost_usd']))
            c.commit()
    except Exception as e:
        log.warning('usage record failed: %s', e)


def usage_rows(days=7):
    from . import store
    from contextlib import closing
    with store._lock, closing(store._db()) as c:
        c.executescript(USAGE_SCHEMA)
    since = time.strftime('%Y-%m-%d', time.gmtime(time.time() - days * 86400))
    return store.q('select * from llm_usage where day>=? order by day desc, cost_usd desc', (since,))


class LLMError(Exception):
    def __init__(self, msg, unavailable=False):
        super().__init__(msg)
        self.unavailable = unavailable


class KeyPool:
    def __init__(self, keys):
        self.keys = list(keys)
        self.i = 0
        self.cool = {}

    def order(self):
        n = len(self.keys)
        idx = [(self.i + k) % n for k in range(n)]
        self.i = (self.i + 1) % max(n, 1)
        now = time.time()
        ready = [j for j in idx if self.cool.get(j, 0) <= now]
        return ready or sorted(idx, key=lambda j: self.cool.get(j, 0))

    def penalise(self, j, seconds):
        self.cool[j] = time.time() + seconds


# ---------------------------------------------------------------- format conversion (OpenAI <-> Anthropic)
def to_anthropic(messages, tools):
    system, out = [], []

    def push(role, blocks):
        if out and out[-1]['role'] == role:
            out[-1]['content'].extend(blocks)
        else:
            out.append({'role': role, 'content': list(blocks)})
    for m in messages:
        r = m['role']
        if r == 'system':
            system.append(m['content'])
        elif r == 'user':
            push('user', [{'type': 'text', 'text': m['content'] or '…'}])
        elif r == 'assistant':
            if m.get('_anthropic_content'):
                push('assistant', m['_anthropic_content'])
                continue
            blocks = [{'type': 'text', 'text': m['content']}] if m.get('content') else []
            for c in m.get('tool_calls') or []:
                try:
                    inp = json.loads(c['function'].get('arguments') or '{}')
                except ValueError:
                    inp = {}
                blocks.append({'type': 'tool_use', 'id': c['id'], 'name': c['function']['name'], 'input': inp})
            push('assistant', blocks or [{'type': 'text', 'text': '…'}])
        elif r == 'tool':
            push('user', [{'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}])
    if out and out[0]['role'] != 'user':
        out.insert(0, {'role': 'user', 'content': [{'type': 'text', 'text': '…'}]})
    atools = [{'name': t['function']['name'], 'description': t['function']['description'][:1000], 'input_schema': t['function']['parameters']} for t in tools or []]
    return system, out, atools


def from_anthropic(d):
    text = ''.join(b.get('text', '') for b in d.get('content', []) if b.get('type') == 'text')
    calls = [{'id': b['id'], 'type': 'function', 'function': {'name': b['name'], 'arguments': json.dumps(b.get('input') or {}, ensure_ascii=False)}}
             for b in d.get('content', []) if b.get('type') == 'tool_use']
    if d.get('stop_reason') == 'refusal':
        text = text or 'این درخواست توسط مدل رد شد.'
    msg = {'role': 'assistant', 'content': text or None, '_anthropic_content': d.get('content', [])}
    if calls:
        msg['tool_calls'] = calls
    return msg


class LLM:
    def __init__(self, keys=None, base=None, model=None, transport=None):
        self.pool = KeyPool(keys if keys is not None else settings.box_keys)
        self.base = (base or settings.box_base).rstrip('/')
        self.model = model or settings.model
        self.transport = transport
        self.stats = {'calls': 0, 'errors': 0, 'cost': 0.0, 'tokens': 0, 'fallbacks': 0, 'by_provider': {}}
        self.gate = {'last_status': None, 'last_error': None, 'last_ok': None, 'window_end': GATE_WINDOW_END}

    # provider selection: runtime toggle (dashboard, stored in DB) > env LLM_PROVIDER > anthropic
    def provider(self):
        try:
            from . import store
            p = store.kv_get('llm_provider')
        except Exception:
            p = None
        p = p or os.environ.get('LLM_PROVIDER', 'anthropic')
        if p == 'anthropic' and not (GATE_URL and GATE_TOKEN):
            p = 'boxapi'
        return p

    def model_of(self, p):
        return ANTHROPIC_MODEL if p == 'anthropic' else self.model

    @property
    def available(self):
        try:
            from .control import ai_enabled
            if not ai_enabled():
                return False
        except Exception:
            pass
        return bool(self.pool.keys) or bool(GATE_URL and GATE_TOKEN)

    def info(self):
        p = self.provider()
        return {'provider': p, 'model': self.model_of(p), 'prompt_cache': prompt_cache_on(), 'fallback': 'boxapi' if p == 'anthropic' else ('anthropic' if GATE_URL and GATE_TOKEN else None),
                'anthropic_configured': bool(GATE_URL and GATE_TOKEN), 'boxapi_keys': len(self.pool.keys), 'gate': self.gate, 'stats': self.stats}

    async def chat(self, messages, tools=None, max_tokens=1800, reasoning=None, timeout=90, json_mode=False):
        from .control import ai_enabled
        if not ai_enabled():  # owner switched to rule-based mode: no LLM call may leave the process
            self.stats['blocked_ai_off'] = self.stats.get('blocked_ai_off', 0) + 1
            raise LLMError('AI is switched off by the owner (rule-based mode)')
        try:
            from .alerts import llm_budget_ok
            if not llm_budget_ok(feature.get()):
                raise LLMError(f'daily LLM budget reached for feature {feature.get()}')
        except LLMError:
            raise
        except Exception:
            pass
        first = self.provider()
        order = [first] + [p for p in ('anthropic', 'boxapi') if p != first and (p == 'boxapi' and self.pool.keys or p == 'anthropic' and GATE_URL and GATE_TOKEN)]
        last = None
        from .resilience import CircuitOpen, breaker
        from .obs import event
        for i, p in enumerate(order):
            br = breaker(f'llm-{p}', threshold=3, cooldown=60)
            t0 = time.time()
            try:
                br.check()
                fn = self._anthropic if p == 'anthropic' else self._boxapi
                msg = await fn(messages, tools, max_tokens, reasoning, timeout, json_mode)
                br.ok()
                us = msg.get('_usage') or {}
                event('llm.call', 'info', f'{p} ok', provider=p, model=self.model_of(p), duration_ms=round((time.time() - t0) * 1000), attempt=i + 1,
                      feature=feature.get(), tokens_in=us.get('tokens_in'), tokens_out=us.get('tokens_out'), cache_read=us.get('cache_read'),
                      cache_write=us.get('cache_write'), cost=us.get('cost_usd'))
                if us:
                    record_usage(p, self.model_of(p), us)
                bp = self.stats['by_provider'].setdefault(p, 0)
                self.stats['by_provider'][p] = bp + 1
                if i:
                    self.stats['fallbacks'] += 1
                msg['_provider'] = p
                return msg
            except CircuitOpen as e:
                last = LLMError(str(e), unavailable=True)
                continue
            except LLMError as e:
                last = e
                event('llm.call', 'warn' if e.unavailable else 'error', f'{p} failed', provider=p, model=self.model_of(p), error=str(e)[:200],
                      duration_ms=round((time.time() - t0) * 1000), attempt=i + 1)
                if not e.unavailable:
                    raise
                br.fail()
        self.stats['errors'] += 1
        raise last or LLMError('no LLM provider configured')

    async def _anthropic(self, messages, tools, max_tokens, reasoning, timeout, json_mode):
        parts, msgs, atools = to_anthropic(messages, tools)
        if json_mode:
            parts = parts + ['Respond with a single valid JSON object only, no prose and no code fences.']
        body = {'model': ANTHROPIC_MODEL, 'max_tokens': min(int(max_tokens), 5000), 'messages': msgs}
        cache = prompt_cache_on()
        if parts:
            # first system part = stable prompt (cached together with the tool definitions); later parts = per-turn context
            blocks = [{'type': 'text', 'text': p} for p in parts if p]
            if cache and blocks:
                blocks[0]['cache_control'] = {'type': 'ephemeral'}
            body['system'] = blocks
        if atools:
            if cache:
                atools = [*atools[:-1], {**atools[-1], 'cache_control': {'type': 'ephemeral'}}]
            body['tools'] = atools
            # no reasoning tokens inside the interactive tool loop (model-specific switch)
            if ANTHROPIC_MODEL.startswith('claude-sonnet-5-5'):
                body['thinking'] = {'type': 'between_tools'}
            elif ANTHROPIC_MODEL.startswith('claude-haiku-5-5'):
                body['thinking'] = {'type': 'disabled'}
        self.stats['calls'] += 1
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=timeout) as c:
                r = await c.post(GATE_URL + '/v1/messages', json=body, headers={'Authorization': f'Bearer {GATE_TOKEN}', 'anthropic-version': '2023-06-01'})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            self.gate.update(last_status=0, last_error=type(e).__name__)
            raise LLMError(f'gate {type(e).__name__}', unavailable=True)
        self.gate['last_status'] = r.status_code
        if r.status_code in (401, 402, 403, 429, 503) or r.status_code >= 500:
            reason = {403: 'پنجره‌ی زمانی دسترسی تمام شده یا دسترسی رد شد', 503: 'کلید قطع اضطراری (kill switch) فعال است', 429: 'محدودیت نرخ'}.get(r.status_code, f'HTTP {r.status_code}')
            self.gate['last_error'] = reason
            raise LLMError(f'anthropic gate {r.status_code}', unavailable=True)
        if r.status_code >= 400:
            if 'thinking' in body and r.status_code == 400:  # older gateway/model without between_tools: retry once without it
                body.pop('thinking')
                async with httpx.AsyncClient(transport=self.transport, timeout=timeout) as c:
                    r = await c.post(GATE_URL + '/v1/messages', json=body, headers={'Authorization': f'Bearer {GATE_TOKEN}', 'anthropic-version': '2023-06-01'})
            if r.status_code >= 400:
                self.gate['last_error'] = f'HTTP {r.status_code}'
                raise LLMError(f'anthropic {r.status_code}: {r.text[:300]}', unavailable=r.status_code >= 500)
        d = r.json()
        self.gate.update(last_ok=time.time(), last_error=None)
        u = d.get('usage') or {}
        pin, pout, pcr, pcw = PRICES.get(ANTHROPIC_MODEL, (0, 0, 0, 0))
        usage = {'tokens_in': u.get('input_tokens') or 0, 'tokens_out': u.get('output_tokens') or 0, 'cache_read': u.get('cache_read_input_tokens') or 0,
                 'cache_write': u.get('cache_creation_input_tokens') or 0}
        usage['cost_usd'] = round((usage['tokens_in'] * pin + usage['tokens_out'] * pout + usage['cache_read'] * pcr + usage['cache_write'] * pcw) / 1e6, 7)
        self.stats['tokens'] += usage['tokens_in'] + usage['tokens_out'] + usage['cache_read'] + usage['cache_write']
        self.stats['cost'] += usage['cost_usd']
        m = from_anthropic(d)
        m['_usage'] = usage
        return m

    async def _boxapi(self, messages, tools, max_tokens, reasoning, timeout, json_mode):
        if not self.pool.keys:
            raise LLMError('no BoxAPI keys configured', unavailable=True)
        clean_msgs = [{k: v for k, v in m.items() if not k.startswith('_')} for m in messages]
        body = {'model': self.model, 'messages': clean_msgs, 'max_completion_tokens': max_tokens}
        if tools:
            body['tools'] = tools
            body['reasoning_effort'] = 'none'
        elif reasoning:
            body['reasoning_effort'] = reasoning
        if json_mode:
            body['response_format'] = {'type': 'json_object'}
        last = None
        for j in self.pool.order():
            key = self.pool.keys[j]
            try:
                self.stats['calls'] += 1
                async with httpx.AsyncClient(transport=self.transport, timeout=timeout) as c:
                    r = await c.post(self.base + '/chat/completions', json=body, headers={'Authorization': f'Bearer {key}'})
                if r.status_code in (401, 402, 403):
                    self.pool.penalise(j, 600); last = f'key {j} rejected ({r.status_code})'; continue
                if r.status_code == 429 or r.status_code >= 500:
                    self.pool.penalise(j, 30); last = f'key {j} busy ({r.status_code})'; continue
                if r.status_code >= 400:
                    if 'credit' in r.text.lower() or 'quota' in r.text.lower() or 'balance' in r.text.lower():
                        raise LLMError(f'BoxAPI credit: {r.text[:200]}', unavailable=True)
                    raise LLMError(f'BoxAPI {r.status_code}: {r.text[:300]}')
                d = r.json()
                u = d.get('usage') or {}
                self.stats['tokens'] += u.get('total_tokens', 0); self.stats['cost'] += float(u.get('cost') or 0)
                m = d['choices'][0]['message']
                m['_usage'] = {'tokens_in': u.get('prompt_tokens') or 0, 'tokens_out': u.get('completion_tokens') or 0,
                               'cache_read': ((u.get('prompt_tokens_details') or {}).get('cached_tokens') or 0), 'cache_write': 0, 'cost_usd': float(u.get('cost') or 0)}
                return m
            except (httpx.TimeoutException, httpx.TransportError) as e:
                self.pool.penalise(j, 20); last = f'key {j} {type(e).__name__}'
                continue
        raise LLMError(f'all BoxAPI keys failed: {last}', unavailable=True)

    async def complete_json(self, system, user, max_tokens=2500, reasoning='low'):
        msg = await self.chat([{'role': 'system', 'content': system}, {'role': 'user', 'content': user}], max_tokens=max_tokens, reasoning=reasoning, json_mode=True, timeout=150)
        txt = (msg.get('content') or '').strip()
        if txt.startswith('```'):
            txt = txt.strip('`').split('\n', 1)[1] if '\n' in txt else txt
        try:
            return json.loads(txt)
        except ValueError:
            s, e = txt.find('{'), txt.rfind('}')
            if s >= 0 and e > s:
                return json.loads(txt[s:e + 1])
            raise LLMError('model did not return JSON')


llm = LLM()
