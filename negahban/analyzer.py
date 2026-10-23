"""Project analyser for the new-project wizard: accepts an uploaded .zip or a public https git URL, detects platform,
port, env vars, databases and estimates resources, then recommends a Liara plan with live price. Archives are untrusted:
zip-slip/symlink/size limits, no code from them is executed."""
import asyncio, io, json, os, re, shutil, subprocess, tempfile, zipfile
from pathlib import Path
from . import plans, store
from .config import settings

MAX_ZIP = 60 * 1024 * 1024
MAX_FILES = 6000


def project_dir(pid):
    if not re.fullmatch(r'proj_[0-9a-f]{12}', pid or ''):
        raise ValueError('bad project id')
    return settings.data_dir / 'projects' / pid / 'src'


def _new():
    pid = store.uid('proj_')
    d = project_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    return pid, d


def extract_zip(data):
    if len(data) > MAX_ZIP:
        raise ValueError('فایل بزرگ‌تر از ۶۰ مگابایت است')
    pid, d = _new()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.infolist()
        if len(names) > MAX_FILES:
            raise ValueError('تعداد فایل‌ها بیش از حد است')
        total = 0
        # strip a single top-level folder (GitHub "Download ZIP")
        tops = {n.filename.split('/')[0] for n in names if n.filename.strip('/')}
        top = next(iter(tops)) if len(tops) == 1 else None
        strip = top + '/' if top and all(n.filename.startswith(top + '/') for n in names if not n.is_dir()) else ''
        for n in names:
            rel = n.filename[len(strip):] if strip and n.filename.startswith(strip) else n.filename
            if not rel or rel.endswith('/'):
                continue
            if (n.external_attr >> 16) & 0o170000 == 0o120000:
                continue  # symlink
            target = (d / rel).resolve()
            if not str(target).startswith(str(d.resolve()) + os.sep):
                continue  # zip-slip
            total += n.file_size
            if total > 200 * 1024 * 1024:
                raise ValueError('حجم پس از استخراج بیش از حد است')
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
    return pid, d


async def clone_git(url):
    if not re.fullmatch(r'https://(github\.com|gitlab\.com|bitbucket\.org|git\.liara\.ir|[a-z0-9.-]+)/[\w.\-/]+?(\.git)?/?', url or ''):
        raise ValueError('فقط آدرس https مخزن عمومی پذیرفته می‌شود')
    pid, d = _new()
    shutil.rmtree(d)
    env = {'PATH': '/usr/bin:/bin', 'GIT_TERMINAL_PROMPT': '0', 'HOME': '/tmp'}
    proc = await asyncio.create_subprocess_exec('git', 'clone', '--depth', '1', '--single-branch', '-c', 'core.symlinks=false', url, str(d),
                                                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 90)
    except asyncio.TimeoutError:
        proc.kill()
        raise ValueError('دریافت مخزن بیش از حد طول کشید')
    if proc.returncode:
        raise ValueError('دریافت مخزن ناموفق بود: ' + out.decode(errors='replace')[-200:])
    shutil.rmtree(d / '.git', ignore_errors=True)
    return pid, d


def _read(p, n=200000):
    try:
        return p.read_text(encoding='utf-8', errors='replace')[:n]
    except OSError:
        return ''


def analyze_dir(d):
    d = Path(d)
    files = [p for p in d.rglob('*') if p.is_file() and 'node_modules' not in p.parts and '.git' not in p.parts][:MAX_FILES]
    rel = {str(p.relative_to(d)) for p in files}
    size_mb = sum(p.stat().st_size for p in files) / 1e6
    has = lambda n: n in rel
    pkg = json.loads(_read(d / 'package.json') or '{}') if has('package.json') else {}
    deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})} if pkg else {}
    req = (_read(d / 'requirements.txt') + _read(d / 'pyproject.toml')).lower()
    platform, framework, port, start = 'docker', None, None, None
    notes = []
    if has('Dockerfile'):
        framework = 'Dockerfile'
        m = re.search(r'(?im)^\s*EXPOSE\s+(\d+)', _read(d / 'Dockerfile'))
        port = int(m.group(1)) if m else None
        if not port:
            m = re.search(r'--port[ =](\d+)|-p\s*(\d+)|PORT[ =](\d+)', _read(d / 'Dockerfile'))
            port = int(next(g for g in m.groups() if g)) if m else 8080
            notes.append('در Dockerfile دستور EXPOSE نبود؛ پورت از دستور اجرا حدس زده شد')
    elif 'next' in deps:
        platform, framework, port = 'next', 'Next.js', 3000
    elif pkg:
        platform, framework, port = 'node', 'Node.js' + (' (Express)' if 'express' in deps else ''), 3000
        if any(x in deps for x in ('react-scripts', 'vite')) and 'express' not in deps:
            platform, framework, port = ('react' if 'react' in deps else 'vue' if 'vue' in deps else 'static'), 'SPA (' + ('React' if 'react' in deps else 'Vite') + ')', 80
    elif has('manage.py') or 'django' in req:
        platform, framework, port = 'django', 'Django', 8000
    elif 'fastapi' in req:
        platform, framework, port = 'docker', 'FastAPI', 8000
        notes.append('برای FastAPI یک Dockerfile استاندارد ساخته می‌شود')
    elif 'flask' in req:
        platform, framework, port = 'flask', 'Flask', 8000
    elif has('composer.json'):
        platform, framework, port = ('laravel' if 'laravel' in _read(d / 'composer.json') else 'php'), 'PHP/Laravel', 80
    elif has('go.mod'):
        platform, framework, port = 'go', 'Go', 8080
    elif any(r.endswith('.csproj') for r in rel):
        platform, framework, port = 'dotnet', '.NET', 80
    elif has('index.html'):
        platform, framework, port = 'static', 'سایت ایستا (HTML)', 80
    else:
        notes.append('نوع پروژه تشخیص داده نشد')
    code = ''.join(_read(p, 60000) for p in files if p.suffix in ('.py', '.js', '.ts', '.php', '.go', '.env', '.example') or p.name in ('.env.example', 'Dockerfile'))[:3_000_000]
    envs = sorted(set(re.findall(r'os\.environ(?:\.get)?\(\s*["\']([A-Z][A-Z0-9_]+)', code) + re.findall(r'os\.environ\[\s*["\']([A-Z][A-Z0-9_]+)', code) +
                      re.findall(r'getenv\(\s*["\']([A-Z][A-Z0-9_]+)', code) + re.findall(r'process\.env\.([A-Z][A-Z0-9_]+)', code) +
                      re.findall(r'(?m)^([A-Z][A-Z0-9_]+)=', _read(d / '.env.example'))))[:40]
    dbs = sorted({n for n, rx in (('PostgreSQL', r'psycopg|pg\b|postgres'), ('MySQL', r'mysql|pymysql'), ('MongoDB', r'mongo'), ('Redis', r'\bredis'), ('SQLite', r'sqlite'))
                  if re.search(rx, req + ' '.join(deps) + code[:400000], re.I)})
    heavy = any(x in req for x in ('torch', 'tensorflow', 'transformers', 'pandas', 'scikit')) or 'puppeteer' in deps or 'playwright' in req
    cpu, ram = 0.3, 0.35
    if platform in ('static', 'react', 'vue'):
        cpu, ram = 0.1, 0.15
    if platform in ('next', 'django', 'laravel'):
        cpu, ram = 0.4, 0.6
    if heavy:
        cpu, ram = 1.2, 2.5
        notes.append('کتابخانه‌های سنگین (یادگیری ماشین/پردازش داده) دیده شد')
    if 'SQLite' in dbs:
        notes.append('SQLite دیده شد: برای ماندگاری داده یک دیسک لیارا لازم است (در غیر این صورت با هر استقرار پاک می‌شود)')
    plan = plans.recommend(cpu, ram)
    return {'platform': platform, 'framework': framework, 'port': port or 8080, 'env_vars': envs, 'databases': dbs, 'files': len(files), 'size_mb': round(size_mb, 2),
            'has_dockerfile': has('Dockerfile'), 'estimate': {'cpu': cpu, 'ram_gb': ram}, 'recommended_plan': plan, 'notes': notes}


def ensure_dockerfile(d, a):
    d = Path(d)
    if (d / 'Dockerfile').exists() or a['platform'] != 'docker':
        return False
    if a['framework'] == 'FastAPI':
        mod = 'main:app' if (d / 'main.py').exists() else 'app.main:app' if (d / 'app' / 'main.py').exists() else 'app:app'
        (d / 'Dockerfile').write_text(f'FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\nEXPOSE 8000\nCMD ["uvicorn","{mod}","--host","0.0.0.0","--port","8000"]\n')
        return True
    return False


async def describe(pid, d):
    await plans.catalogue()
    a = analyze_dir(d)
    a['dockerfile_generated'] = ensure_dockerfile(d, a)
    if a['dockerfile_generated']:
        a['platform'] = 'docker'
    p = a['recommended_plan']
    a['pricing'] = {pid_: {'hour': plans.hourly(pid_, 'basic'), 'month': round(plans.hourly(pid_, 'basic') * 720), 'cpu': plans.plan_sync(pid_)['cpu'], 'ram': plans.plan_sync(pid_)['ram'],
                           'name_fa': plans.plan_sync(pid_)['name_fa']} for pid_ in plans.ORDER[:4]}
    a['recommendation_fa'] = (f"پیشنهاد: پلن {plans.plan_sync(p)['name_fa']} ({p}) با باندل برنزی — "
                              f"{plans.fmt_toman(plans.hourly(p, 'basic'))} در ساعت (حدود {plans.fmt_toman(plans.hourly(p, 'basic') * 720)} در ماه). "
                              f"برآورد نیاز: {a['estimate']['cpu']} هسته و {a['estimate']['ram_gb']} گیگ رم با ۳۰٪ حاشیه‌ی اطمینان.")
    from .redact import SENSITIVE_ENV
    a['env_schema'] = [{'name': n, 'secret': bool(SENSITIVE_ENV.search(n)), 'required': True} for n in a['env_vars']]
    a['bundle'] = 'basic'
    a['reasoning'] = [f"نوع پروژه: {a['framework'] or 'نامشخص'} ({a['files']} فایل، {a['size_mb']} مگابایت)",
                      'Dockerfile پروژه استفاده می‌شود' if a['has_dockerfile'] else ('یک Dockerfile استاندارد ساخته شد' if a['dockerfile_generated'] else f"پلتفرم بومی لیارا: {a['platform']}"),
                      f"پورت برنامه: {a['port']}",
                      f"برآورد منابع: {a['estimate']['cpu']} هسته و {a['estimate']['ram_gb']} گیگ رم؛ کوچک‌ترین پلنی که ۳۰٪ حاشیه دارد {p} است",
                      'باندل برنزی (basic) کافی است: ۲۰ استقرار در روز و SSL رایگان؛ برای استقرار بدون قطعی، نقره‌ای لازم است']
    if a['databases']:
        a['reasoning'].append(f"پایگاه داده: {'، '.join(a['databases'])} — از دیتابیس مدیریت‌شده‌ی لیارا (DBaaS) استفاده کنید و آدرس آن را در متغیرها بگذارید")
    if a['env_vars']:
        a['reasoning'].append(f"{len(a['env_vars'])} متغیر محیطی لازم است ({sum(1 for e in a['env_schema'] if e['secret'])} مورد محرمانه)")
    a['health_path'] = analyze_health(d) or '/'
    a['project_id'] = pid
    store.kv_set(f'project:{pid}', {'port': a['port'], 'platform': a['platform'], 'health_path': a['health_path'], 'framework': a['framework']})
    return a


def analyze_health(d):
    """Find a health route in the source (best effort) so verification probes the right path."""
    import re as _re
    for p in Path(d).rglob('*'):
        if p.is_file() and p.suffix in ('.py', '.js', '.ts', '.go') and p.stat().st_size < 400_000 and 'node_modules' not in p.parts:
            m = _re.search(r'''["'](/health[a-z]*)/?["']''', _read(p, 200000))
            if m:
                return m.group(1)
    return None


async def analyze_git(url):
    pid, d = await clone_git(url)
    return await describe(pid, d)


async def analyze_zip(data):
    pid, d = extract_zip(data)
    return await describe(pid, d)
