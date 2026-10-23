"""Deploy, rollback and archive redeploys. Every source Negahban uploads is archived locally first, so a rollback is always
possible even where Liara's (console-only) rollback endpoint refuses."""
import time
from pathlib import Path
from . import bus, store
from . import workspace as ws
from .liara import LiaraError, liara


async def _wait(app, rid, ctx):
    """Wait for the release while streaming its build output live to the dashboard (op events, last lines)."""
    import asyncio
    state = {'st': None, 'since': '', 'n': 0}

    async def on_state(st):
        state['st'] = st
        bus.publish('op', proposal_id=ctx.get('proposal_id'), app=app, status='running', text=f'وضعیت ریلیز: {st}')

    async def stream():
        from .sanitize import clean
        while state['st'] not in ('READY', 'FAILED', 'CANCELED', 'TIMEDOUT', 'UNHEALTHY'):
            try:
                d = await liara.build_logs(rid, state['since'])
                out = d.get('buildOutput') or []
                if out and out[-1].get('createdAt') != state['since']:
                    state['since'] = out[-1].get('createdAt') or state['since']
                    lines = [clean(x.get('line', ''), 200).strip() for x in out if (x.get('line') or '').strip()]
                    state['n'] += len(lines)
                    if lines:
                        bus.publish('op', proposal_id=ctx.get('proposal_id'), app=app, status='running', text='🔨 بیلد: ' + lines[-1][:180], build_lines=lines[-6:])
            except Exception:
                pass
            await asyncio.sleep(4)
    task = asyncio.create_task(stream())
    try:
        st, rel = await liara.wait_release(app, rid, timeout=600, on_state=on_state)
    finally:
        state['st'] = state['st'] or 'DONE'
        task.cancel()
    return st, rel


async def _release_settings(app):
    """Keep port/disks of the current release (envs are left to Liara: omitted envs keep the app's current set)."""
    reg = store.get_app(app) or {}
    port, disks = reg.get('port') or 8080, None
    try:
        rels = (await liara.releases(app, 3)).get('releases', [])
        cur = next((r for r in rels if r.get('state') == 'READY'), None)
        if cur:
            port = cur.get('port') or port
            disks = [{'name': d['name'], 'mountTo': d['mountTo']} for d in cur.get('disks') or []] or None
    except LiaraError:
        pass
    return port, disks


async def deploy_tar(app, tb, message, ctx, origin, commit=None):
    port, disks = await _release_settings(app)
    aid = ws.archive_bytes(app, tb, message, origin, commit)
    bus.publish('op', proposal_id=ctx.get('proposal_id'), app=app, status='running', text='بارگذاری سورس در لیارا…')
    rid = await liara.deploy_tar(app, tb, port, None, disks, message)
    store.x('update archive set release_id=? where id=?', (rid, aid))
    st, rel = await _wait(app, rid, ctx)
    store.set_archive_state(aid, st)
    return {'release_id': rid, 'release_state': st, 'archive_id': aid, 'commit': commit, 'failReason': (rel or {}).get('failReason')}


async def deploy_workspace(app, message, ctx):
    ws.ensure(app)
    return await deploy_tar(app, ws.tarball(app), message, ctx, 'workspace', ws.head(app))


async def redeploy_archive(app, archive_id, ctx):
    a = store.q('select * from archive where id=? and app=?', (archive_id, app), one=True)
    if not a or not Path(a['path']).exists():
        raise ValueError('archive not found')
    return await deploy_tar(app, Path(a['path']).read_bytes(), f"redeploy archive {a['message'] or ''}"[:120], ctx, 'archive', a.get('commit_sha'))


async def rollback(app, release_id, ctx):
    """1) Liara rollback API to the newest READY release that is not current; 2) fallback: redeploy the previous archive."""
    data = await liara.releases(app, 15)
    cur = data.get('currentRelease')
    rels = data.get('releases', [])
    target = None
    if release_id:
        target = next((r for r in rels if r['_id'] == release_id), None)
    else:
        idx = next((i for i, r in enumerate(rels) if r['_id'] == cur), 0)
        target = next((r for r in rels[idx + 1:] if r.get('state') == 'READY' and r.get('type') in ('DEPLOY', 'ENV_UPDATE')), None)
    if target:
        try:
            bus.publish('op', proposal_id=ctx.get('proposal_id'), app=app, status='running', text=f"بازگشت به ریلیز {target.get('tag')} با API لیارا…")
            await liara.rollback(target['_id'])
            t0 = time.time()
            # rollback creates/activates a release; wait until current changes and is READY
            while time.time() - t0 < 300:
                d = await liara.releases(app, 3)
                top = (d.get('releases') or [{}])[0]
                if d.get('currentRelease') != cur and top.get('state') in ('READY', 'FAILED', 'UNHEALTHY'):
                    return {'method': 'liara_rollback', 'to_tag': target.get('tag'), 'release_state': top.get('state'), 'release_id': d.get('currentRelease')}
                import asyncio
                await asyncio.sleep(5)
            return {'method': 'liara_rollback', 'to_tag': target.get('tag'), 'release_state': 'WAIT_TIMEOUT'}
        except LiaraError as e:
            bus.publish('op', proposal_id=ctx.get('proposal_id'), app=app, status='running', text=f'API بازگشت لیارا پاسخ نداد ({e.status})؛ استفاده از آرشیو سورس…')
    # fallback: previous successful archive
    arcs = [a for a in store.archives(app, 30) if a.get('state') == 'READY']
    if len(arcs) >= 2:
        prev = arcs[1]
        res = await redeploy_archive(app, prev['id'], ctx)
        if (store.get_app(app) or {}).get('role') == 'demo' and prev.get('commit_sha'):
            try:
                ws.git(app, 'revert', '--no-edit', 'HEAD')
            except Exception:
                pass
        return {'method': 'archive_redeploy', 'archive': prev['id'], **res}
    raise ValueError('no earlier healthy release or archive to roll back to')


async def deploy_project(app, project_id, ctx):
    from .analyzer import project_dir
    d = project_dir(project_id)
    from .liara import make_tarball
    tb = make_tarball(d)
    meta = store.kv_get(f'project:{project_id}', {})
    if meta.get('port'):
        store.upsert_app(app, 'monitored', None, meta.get('health_path') or '/', int(meta['port']))
    port = int(meta.get('port') or 8080)
    aid = ws.archive_bytes(app, tb, f'first deploy {project_id}', 'wizard')
    bus.publish('op', proposal_id=ctx.get('proposal_id'), app=app, status='running', text='بارگذاری سورس پروژه در لیارا…')
    rid = await liara.deploy_tar(app, tb, port, meta.get('envs') or None, None, f'first deploy via Negahban ({project_id})')
    store.x('update archive set release_id=? where id=?', (rid, aid))
    st, rel = await _wait(app, rid, ctx)
    store.set_archive_state(aid, st)
    if st == 'READY':  # every app the agent creates is saved as a reusable, secret-free template
        try:
            from .templates import save_from_project
            t = save_from_project(project_id, app)
            if t:
                bus.publish('op', proposal_id=ctx.get('proposal_id'), app=app, status='running', text=f"قالب «{t['name']}» در کتابخانه ذخیره شد")
        except Exception:
            pass
    return {'release_id': rid, 'release_state': st, 'url': f'https://{app}.liara.run', 'failReason': (rel or {}).get('failReason')}
