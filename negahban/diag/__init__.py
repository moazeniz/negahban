"""Negahban diagnostics toolkit: container exec, HTTP/TLS/DNS probes, extra Liara data, analytics, log intelligence,
runbooks and web search for unknown errors. Each module declares tool specs with @common.spec; register(tool) wires them
into the core registry (tools.tool) wrapped with a timeout, an audit row and an output-size cap. See docs/DIAGNOSTICS.md."""
import asyncio, functools, importlib, time

MODULES = ('exec', 'probes', 'liara_data', 'analytics', 'logintel', 'runbooks', 'websearch', 'iaas', 'server_templates', 'dbaas', 'storage', 'addons')
LABELS, SENSITIVE = {}, set()
TYPED_CONFIRM = {'delete_server': 'server', 'delete_database': 'database', 'restore_database_backup': 'database', 'delete_bucket': 'bucket'}  # tool -> arg the owner must type


def all_specs():
    from .common import specs_of
    out = []
    for m in MODULES:
        out.extend(specs_of(importlib.import_module(f'{__name__}.{m}')))
    return out


def _wrap(s):
    from .common import DiagError, audit, shrink

    @functools.wraps(s['fn'])
    async def run(args, ctx):
        t0 = time.perf_counter()
        ok, err = True, None
        try:
            res = await asyncio.wait_for(s['fn'](args, ctx or {}), s['timeout'])
            return shrink(res)
        except asyncio.TimeoutError:
            ok, err = False, f"{s['name']} timed out after {s['timeout']}s"
            raise DiagError(err)
        except Exception as e:
            ok, err = False, f'{type(e).__name__}: {str(e)[:300]}'
            raise
        finally:
            audit(ctx, f"diag.{s['name']}", (args or {}).get('app'),
                  {'args': {k: (v[:300] if isinstance(v, str) else v) for k, v in (args or {}).items()}, 'ms': round((time.perf_counter() - t0) * 1000), 'error': err}, ok)
    return run


def register(tool):
    """Register every diag tool with the core decorator tools.tool(name, risk, desc, params, required, title=, impact=, label_fa=)."""
    for s in all_specs():
        tool(s['name'], s['risk'], s['desc'], s['params'], s['required'], title=s.get('title'), impact=s.get('impact'), label_fa=s['label_fa'])(_wrap(s))
        LABELS[s['name']] = s['label_fa']
        if s['risk'] == 'sensitive':
            SENSITIVE.add(s['name'])


# importing the core registry here (after register() exists) makes `import negahban.diag.<module>` safe in any order
from .. import tools as _tools  # noqa: E402,F401


def limitations():
    """(title_fa, text_fa) rows for the in-app limitations page."""
    import importlib
    out = []
    for m in ('dbaas', 'storage'):
        out += list(getattr(importlib.import_module(f'{__name__}.{m}'), 'LIMITATIONS', []))
    return out
