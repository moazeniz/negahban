"""Git workspaces for apps whose source Negahban manages (the demo app), release archive, and sandboxed test runs."""
import asyncio, hashlib, os, re, shutil, subprocess, tempfile, time
from pathlib import Path
from . import store
from .config import settings
from .liara import make_tarball

GIT_ENV = {'GIT_AUTHOR_NAME': 'Negahban', 'GIT_AUTHOR_EMAIL': 'negahban@localhost', 'GIT_COMMITTER_NAME': 'Negahban',
           'GIT_COMMITTER_EMAIL': 'negahban@localhost', 'HOME': '/tmp', 'GIT_TERMINAL_PROMPT': '0'}
SAFE_EXT = ('.py', '.txt', '.toml', '.cfg', '.ini', '.json', '.html', '.css', '.js', '.md', '.yaml', '.yml')


def ws_dir(app):
    return settings.data_dir / 'workspaces' / app


def git(app, *args, check=True):
    r = subprocess.run(['git', *args], cwd=ws_dir(app), capture_output=True, text=True, timeout=60, env={**os.environ, **GIT_ENV})
    if check and r.returncode:
        raise RuntimeError(f'git {args[0]} failed: {r.stderr[-400:]}')
    return r.stdout.strip()


def ensure(app, seed=None):
    d = ws_dir(app)
    if (d / '.git').exists():
        return d
    seed = Path(seed or settings.demo_src)
    d.parent.mkdir(parents=True, exist_ok=True)
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(seed, d, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache', '*.pyc', '.env*'))
    git(app, 'init', '-q', '-b', 'main')
    git(app, 'add', '-A')
    git(app, 'commit', '-q', '-m', 'نسخه‌ی اولیه (seed)')
    return d


def head(app):
    return git(app, 'rev-parse', '--short', 'HEAD')


def log(app, n=10):
    out = git(app, 'log', f'-{n}', '--pretty=format:%h\x1f%s\x1f%ct\x1f%an')
    return [dict(zip(('sha', 'message', 'ts', 'author'), l.split('\x1f'))) for l in out.splitlines() if l]


def show_last_diff(app, n=1):
    return git(app, 'diff', f'HEAD~{n}', 'HEAD', check=False)[:8000]


def safe_path(app, rel):
    rel = rel.replace('\\', '/').lstrip('/')
    for pre in ('app/', 'usr/src/app/'):
        if rel.startswith(pre) and not (ws_dir(app) / rel).exists():
            rel = rel[len(pre):]
    p = (ws_dir(app) / rel).resolve()
    if not str(p).startswith(str(ws_dir(app).resolve()) + os.sep) or '.git' in p.parts:
        raise ValueError(f'path outside workspace: {rel}')
    return p


def read(app, rel, limit=20000):
    p = safe_path(app, rel)
    return p.read_text(encoding='utf-8', errors='replace')[:limit] if p.is_file() else None


def files(app):
    root = ws_dir(app)
    return sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts)


def write_files(app, changes, message):
    for ch in changes:
        p = safe_path(app, ch['path'])
        if not p.name.endswith(SAFE_EXT) and p.name not in ('Dockerfile', 'requirements.txt', 'Procfile'):
            raise ValueError(f'refusing to write {p.name}')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ch['content'], encoding='utf-8')
    git(app, 'add', '-A')
    git(app, 'commit', '-q', '-m', message)
    return head(app)


def revert_last(app, message=None):
    git(app, 'revert', '--no-edit', 'HEAD')
    if message:
        git(app, 'commit', '--amend', '-q', '-m', message)
    return head(app)


def diff_of(app, changes):
    """Unified diff of proposed changes vs workspace HEAD, computed without touching the workspace."""
    import difflib
    out = []
    for ch in changes:
        old = read(app, ch['path']) or ''
        out.extend(difflib.unified_diff(old.splitlines(True), ch['content'].splitlines(True), f"a/{ch['path']}", f"b/{ch['path']}"))
    return ''.join(out)


async def run_tests(app, changes=None, timeout=90):
    """Sandboxed test run: a throw-away copy of the workspace, scrubbed environment (no secrets), CPU/memory/time
    limits, and no network namespace when `unshare` is permitted. The LLM never gets a shell; only this fixed command runs."""
    src = ws_dir(app)
    tmp = Path(tempfile.mkdtemp(prefix='ngh-test-'))
    try:
        shutil.copytree(src, tmp / 'w', ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache'))
        for ch in changes or []:
            rel = str(safe_path(app, ch['path']).relative_to(src.resolve()))
            (tmp / 'w' / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / 'w' / rel).write_text(ch['content'], encoding='utf-8')
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(tmp), 'PYTHONDONTWRITEBYTECODE': '1', 'LANG': 'C.UTF-8'}  # no secrets: tests bring their own dummy values
        import sys
        cmd = [sys.executable, '-m', 'pytest', '-q', '--no-header', '-p', 'no:cacheprovider', '-W', 'ignore::DeprecationWarning']
        limits = 'ulimit -v 1500000; ulimit -t 120; ulimit -f 20000; '
        sh = limits + ' '.join(cmd)
        argv = ['sh', '-c', sh]
        if shutil.which('unshare') and _can_unshare():
            argv = ['unshare', '-rn', 'sh', '-c', sh]
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(*argv, cwd=tmp / 'w', env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {'passed': False, 'output': 'timeout', 'seconds': timeout, 'summary': 'زمان اجرای تست تمام شد'}
        text = out.decode(errors='replace')[-6000:]
        m = re.search(r'(\d+) passed', text); f = re.search(r'(\d+) failed', text); e = re.search(r'(\d+) error', text)
        return {'passed': proc.returncode == 0, 'output': text, 'seconds': round(time.time() - t0, 1), 'isolated_network': argv[0] == 'unshare',
                'summary': f"{m.group(1) if m else 0} موفق، {f.group(1) if f else 0} ناموفق" + (f"، {e.group(1)} خطا" if e else '')}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_unshare_ok = None


def _can_unshare():
    global _unshare_ok
    if _unshare_ok is None:
        try:
            _unshare_ok = subprocess.run(['unshare', '-rn', 'true'], capture_output=True, timeout=5).returncode == 0
        except Exception:
            _unshare_ok = False
    return _unshare_ok


# ---- release archive -----------------------------------------------------------
def archive_bytes(app, tar_bytes, message, origin, commit_sha=None, release_id=None):
    d = settings.data_dir / 'archive' / app
    d.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(tar_bytes).hexdigest()
    aid = store.uid('arc_')
    p = d / f'{int(time.time())}-{sha[:10]}.tar.gz'
    p.write_bytes(tar_bytes)
    store.add_archive(id=aid, app=app, release_id=release_id, sha256=sha, path=str(p), message=message, commit_sha=commit_sha, origin=origin, size=len(tar_bytes))
    # keep the 25 newest archives per app
    for old in store.archives(app, 200)[25:]:
        try:
            Path(old['path']).unlink(missing_ok=True)
        except OSError:
            pass
        store.x('delete from archive where id=?', (old['id'],))
    return aid


def tarball(app):
    return make_tarball(ws_dir(app), extra_skip=('tests',))
