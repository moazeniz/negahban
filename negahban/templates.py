"""Liara-native deploy templates (docker platform, no Kubernetes): Dockerfile + liara.json + a minimal starter source, an
env-var schema (names only for secrets, values never stored), recommended plan/bundle, autopilot bounds and an optional disk.
Builtins live in code + template_files/ and are read-only; custom templates live in SQLite with a full version history
(every edit = new version, restore = new version with old content). Rendering is plain {{param}} substitution."""
import json, math, re, time
from pathlib import Path
from . import plans, store
from .config import settings

FILES_DIR = Path(__file__).resolve().parent / 'template_files'
BUNDLES = ('basic', 'standard', 'pro')
RESERVED_PARAMS = ('app_name', 'port', 'health_path')
SYSTEM_KEYS = ('id', 'builtin', 'version', 'origin', 'created_at', 'updated_at', 'source_app')
EDITABLE = ('name', 'stack', 'description_fa', 'port', 'health_path', 'health_check', 'cron', 'params', 'env', 'plan', 'bundle',
            'autoscale', 'disk', 'files')
ID_RX = re.compile(r'^[a-z0-9][a-z0-9_-]{1,40}$')
APP_RX = re.compile(r'^[a-z0-9][a-z0-9-]{2,30}$')
ENV_RX = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
PARAM_RX = re.compile(r'^[a-z_][a-z0-9_]{0,30}$')
PATH_RX = re.compile(r'^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[\w.@+\-/]{1,160}$')
CRON_RX = re.compile(r'^(\S+\s+){5}\S.{0,300}$')
SUB_RX = re.compile(r'\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}')
SECRET_NAME = re.compile(r'(?i)(token|secret|password|passwd|pass$|key$|_key|credential|private|dsn|database_url|redis_url|mongo.*url|amqp_url)')
MAX_FILES, MAX_FILE, MAX_TOTAL = 80, 200_000, 1_000_000


class TemplateError(ValueError):
    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__('\n'.join(self.errors))


# ------------------------------------------------------------------ storage
_ready = set()


def _ensure():
    key = str(settings.data_dir)
    if key in _ready:
        return
    store.x('create table if not exists templates(id text primary key, name text, stack text, version int, data text, origin text, created_at real, updated_at real)')
    store.x('create table if not exists template_versions(template_id text, version int, data text, ts real, actor text, note text, primary key(template_id, version))')
    _ready.add(key)


# ------------------------------------------------------------------ builtins
def _load_dir(name, *shared):
    root = FILES_DIR / name
    out = {p.relative_to(root).as_posix(): p.read_text(encoding='utf-8') for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}
    for s in shared:
        out[s] = (FILES_DIR / '_shared' / s).read_text(encoding='utf-8')
    return out


def _hc(cmd, start=20):
    return {'command': cmd, 'interval': 30, 'timeout': 5, 'retries': 3, 'startPeriod': start}


def _e(name, description, default='', required=False, secret=False):
    return {'name': name, 'required': required, 'secret': secret, 'default': default, 'description': description}


def _auto(lo, hi, day):
    return {'min_plan': lo, 'max_plan': hi, 'max_toman_day': day}


WGET = 'wget -q -O /dev/null http://127.0.0.1:{{port}}{{health_path}}'
_BUILTIN_META = [
    dict(id='fastapi', name='FastAPI (پایتون)', stack='fastapi', port=8000, health_path='/health', files=('fastapi', 'healthcheck.py'),
         description_fa='API پایتونی با FastAPI و uvicorn روی python:3.12-slim. مسیر سلامت /health و بررسی سلامت داخلی لیارا.',
         health_check=_hc('python healthcheck.py'), plan='small-g2', autoscale=_auto('small-g2', 'medium-g2', 60000),
         env=[_e('APP_ENV', 'محیط اجرا', 'production'), _e('WEB_CONCURRENCY', 'تعداد پردازه‌های uvicorn', '1'), _e('SECRET_KEY', 'کلید امضای برنامه', secret=True)]),
    dict(id='node-express', name='Node.js + Express', stack='node', port=3000, health_path='/health', files=('node-express', 'healthcheck.js'),
         description_fa='سرور Node.js 20 با Express. اگر اسکریپت build داشته باشد اجرا می‌شود؛ شروع با npm start.',
         health_check=_hc('node healthcheck.js'), plan='small-g2', autoscale=_auto('small-g2', 'medium-g2', 60000),
         env=[_e('NODE_ENV', 'محیط اجرا', 'production'), _e('SESSION_SECRET', 'کلید نشست کاربران', secret=True)]),
    dict(id='nextjs-static', name='Next.js (خروجی ایستا + nginx)', stack='nextjs', port=80, health_path='/health', files=('nextjs-static', 'nginx.conf'),
         description_fa='سایت Next.js با output: export؛ در مرحله‌ی build ساخته و با nginx سبک سرو می‌شود (بدون سرور Node در زمان اجرا، ارزان‌ترین پلن کافی است).',
         health_check=_hc(WGET, 10), plan='small-g2', autoscale=_auto('small-g2', 'small-g2', 30000),
         env=[_e('NEXT_PUBLIC_SITE_NAME', 'نام سایت (در زمان build خوانده می‌شود)', '{{app_name}}')]),
    dict(id='django-postgres', name='Django + PostgreSQL', stack='django', port=8000, health_path='/health/', files=('django-postgres', 'healthcheck.py'),
         params=[{'name': 'wsgi_module', 'default': 'config.wsgi', 'description': 'ماژول WSGI پروژه'}],
         description_fa='Django 5 با gunicorn؛ پایگاه‌داده از متغیر محرمانه‌ی DATABASE_URL (PostgreSQL لیارا) خوانده می‌شود و migrate هنگام شروع اجرا می‌شود. بدون آن، SQLite موقت.',
         health_check=_hc('python healthcheck.py', 40), plan='medium-g2', autoscale=_auto('medium-g2', 'standard-base-g2', 90000),
         env=[_e('DATABASE_URL', 'آدرس اتصال PostgreSQL لیارا (postgresql://user:pass@host:port/db)', required=True, secret=True),
              _e('DJANGO_SECRET_KEY', 'کلید محرمانه‌ی Django', required=True, secret=True), _e('ALLOWED_HOSTS', 'دامنه‌های مجاز (با کاما)', '{{app_name}}.liara.run'),
              _e('DJANGO_DEBUG', 'حالت دیباگ (0 یا 1)', '0'), _e('WEB_CONCURRENCY', 'تعداد workerهای gunicorn', '2')]),
    dict(id='laravel', name='Laravel (PHP 8.3 + Apache)', stack='php', port=80, health_path='/up', files=('laravel',),
         description_fa='Laravel روی php:8.3-apache با composer؛ هنگام شروع config:cache و migrate اجرا می‌شود. مسیر سلامت /up (پیش‌فرض Laravel 11).',
         health_check=_hc('php /var/www/html/healthcheck.php', 40), plan='medium-g2', autoscale=_auto('medium-g2', 'standard-base-g2', 90000),
         env=[_e('APP_KEY', 'کلید رمزنگاری Laravel (base64:...)', required=True, secret=True), _e('APP_ENV', 'محیط اجرا', 'production'),
              _e('APP_DEBUG', 'حالت دیباگ', 'false'), _e('APP_URL', 'آدرس سایت', 'https://{{app_name}}.liara.run'), _e('LOG_CHANNEL', 'کانال لاگ', 'stderr'),
              _e('DB_CONNECTION', 'نوع پایگاه‌داده', 'mysql'), _e('DB_HOST', 'میزبان پایگاه‌داده'), _e('DB_PORT', 'پورت پایگاه‌داده', '3306'),
              _e('DB_DATABASE', 'نام پایگاه‌داده'), _e('DB_USERNAME', 'نام کاربری پایگاه‌داده'), _e('DB_PASSWORD', 'رمز پایگاه‌داده', secret=True)]),
    dict(id='go', name='Go (net/http)', stack='go', port=8080, health_path='/health', files=('go',),
         description_fa='سرویس Go 1.22 با build چندمرحله‌ای؛ اجرای باینری کوچک روی alpine با کاربر غیر root.',
         health_check=_hc(WGET, 10), plan='small-g2', autoscale=_auto('small-g2', 'medium-g2', 60000), env=[_e('APP_ENV', 'محیط اجرا', 'production')]),
    dict(id='static-site', name='سایت ایستا (nginx)', stack='static', port=80, health_path='/health', files=('static-site', 'nginx.conf'),
         description_fa='سایت HTML/CSS/JS ایستا با nginx:alpine، فشرده‌سازی gzip و کش فایل‌های ثابت.',
         health_check=_hc(WGET, 5), plan='small-g2', autoscale=_auto('small-g2', 'small-g2', 30000), env=[]),
    dict(id='worker-cron', name='Worker پایتون + کران', stack='worker', port=8080, health_path='/health', files=('worker-cron', 'healthcheck.py'),
         params=[{'name': 'data_dir', 'default': '/data', 'description': 'مسیر دیسک برای وضعیت کار'}],
         description_fa='کارگر پس‌زمینه‌ی پایتون: job.py هر JOB_INTERVAL_SECONDS ثانیه اجرا می‌شود (زمان‌بند داخلی) و ورودی cron در liara.json هم آماده است. دیسک data برای نگه‌داشتن وضعیت.',
         health_check=_hc('python healthcheck.py', 10), plan='small-g2', autoscale=_auto('small-g2', 'medium-g2', 50000),
         cron=['*/15 * * * * cd /app && python job.py'], disk={'name': 'data', 'mountTo': '/data', 'size_gb': 1},
         env=[_e('JOB_INTERVAL_SECONDS', 'فاصله‌ی اجرای کار (ثانیه)', '300'), _e('INTERNAL_SCHEDULER', 'زمان‌بند داخلی (۱) یا فقط cron لیارا (۰)', '1'),
              _e('DATA_DIR', 'مسیر داده', '/data'), _e('API_TOKEN', 'توکن سرویسی که job صدا می‌زند', secret=True)]),
    dict(id='docker-generic', name='Docker عمومی', stack='docker', port=8080, health_path='/health', files=('docker-generic',),
         params=[{'name': 'base_image', 'default': 'python:3.12-alpine', 'description': 'ایمیج پایه'}],
         description_fa='Dockerfile عمومی برای هر زبان: ایمیج پایه و CMD را عوض کنید. نمونه‌ی آماده یک سرور فایل ساده روی پورت 8080 است.',
         health_check=_hc(WGET, 10), plan='small-g2', autoscale=_auto('small-g2', 'medium-g2', 60000), env=[]),
]


def _builtin(m):
    d, shared = m['files'][0], m['files'][1:]
    t = {'id': m['id'], 'name': m['name'], 'stack': m['stack'], 'description_fa': m['description_fa'], 'port': m['port'], 'health_path': m['health_path'],
         'health_check': m.get('health_check'), 'cron': m.get('cron', []), 'params': m.get('params', []), 'env': m['env'], 'plan': m['plan'],
         'bundle': 'basic', 'autoscale': m['autoscale'], 'disk': m.get('disk'), 'files': _load_dir(d, *shared)}
    t = validate(t)
    return {**t, 'id': m['id'], 'builtin': True, 'version': 1, 'origin': 'builtin', 'created_at': 0, 'updated_at': 0}


BUILTINS = {}  # filled at the end of the module (validate() must exist first)


# ------------------------------------------------------------------ validation
def _int(v, lo, hi, label, errs):
    try:
        if isinstance(v, bool) or float(v) != int(float(v)):
            raise ValueError
        v = int(float(v))
    except (TypeError, ValueError):
        errs.append(f'{label} باید عدد صحیح باشد'); return None
    if not lo <= v <= hi:
        errs.append(f'{label} باید بین {lo} و {hi} باشد'); return None
    return v


def _str(v, label, errs, mx=200, required=True, rx=None):
    if v is None or v == '':
        if required:
            errs.append(f'{label} خالی است')
        return ''
    if not isinstance(v, str):
        errs.append(f'{label} باید متن باشد'); return ''
    v = v.strip()
    if len(v) > mx or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', v):
        errs.append(f'{label} نامعتبر یا بیش از {mx} نویسه است'); return ''
    if rx and not rx.match(v):
        errs.append(f'{label} قالب نامعتبری دارد: {v[:40]}')
    return v


def validate(data):
    """Normalise and validate the editable part of a template. Raises TemplateError with every problem found (Persian)."""
    if not isinstance(data, dict):
        raise TemplateError('قالب باید یک شیء JSON باشد')
    errs = []
    unknown = set(data) - set(EDITABLE) - set(SYSTEM_KEYS) - {'note'}
    if unknown:
        errs.append(f'کلیدهای ناشناخته: {", ".join(sorted(unknown))}')
    t = {'name': _str(data.get('name'), 'نام قالب', errs, 80),
         'stack': _str(data.get('stack') or 'docker', 'stack', errs, 30, rx=re.compile(r'^[a-z0-9.+-]{1,30}$')),
         'description_fa': _str(data.get('description_fa'), 'توضیح', errs, 1000, required=False)}
    t['port'] = _int(data.get('port', 8080), 1, 65535, 'پورت', errs)
    t['health_path'] = _str(data.get('health_path') or '/', 'مسیر سلامت', errs, 120, rx=re.compile(r'^/[\w./~-]*$'))
    hc = data.get('health_check')
    if hc in (None, {}, ''):
        t['health_check'] = None
    elif not isinstance(hc, dict):
        errs.append('health_check باید شیء باشد'); t['health_check'] = None
    else:
        if set(hc) - {'command', 'interval', 'timeout', 'retries', 'startPeriod'}:
            errs.append('health_check فقط command/interval/timeout/retries/startPeriod دارد')
        t['health_check'] = {'command': _str(hc.get('command'), 'دستور بررسی سلامت', errs, 500),
                             'interval': _int(hc.get('interval', 30), 5, 3600, 'interval', errs), 'timeout': _int(hc.get('timeout', 5), 1, 300, 'timeout', errs),
                             'retries': _int(hc.get('retries', 3), 1, 20, 'retries', errs), 'startPeriod': _int(hc.get('startPeriod', 20), 0, 3600, 'startPeriod', errs)}
    cron = data.get('cron') or []
    if not isinstance(cron, list) or len(cron) > 10:
        errs.append('cron باید فهرستی تا ۱۰ خط باشد'); cron = []
    t['cron'] = [_str(c, 'خط cron', errs, 320, rx=CRON_RX) for c in cron]
    params = data.get('params') or []
    if not isinstance(params, list) or len(params) > 20:
        errs.append('params باید فهرستی تا ۲۰ پارامتر باشد'); params = []
    t['params'], seen = [], set()
    for p in params:
        if not isinstance(p, dict):
            errs.append('هر پارامتر باید شیء {name, default, description} باشد'); continue
        n = _str(p.get('name'), 'نام پارامتر', errs, 31, rx=PARAM_RX)
        if n in RESERVED_PARAMS or n in seen:
            errs.append(f'پارامتر {n} تکراری یا رزرو شده است (app_name/port/health_path خودکارند)'); continue
        seen.add(n)
        t['params'].append({'name': n, 'default': _param_value(n, p.get('default', ''), errs), 'description': _str(p.get('description'), 'توضیح پارامتر', errs, 200, required=False)})
    env = data.get('env') or []
    if not isinstance(env, list) or len(env) > 60:
        errs.append('env باید فهرستی تا ۶۰ متغیر باشد'); env = []
    t['env'], seen = [], set()
    for e in env:
        if not isinstance(e, dict):
            errs.append('هر متغیر محیطی باید شیء {name, required, secret, default, description} باشد'); continue
        n = _str(e.get('name'), 'نام متغیر', errs, 64, rx=ENV_RX)
        if n in seen:
            errs.append(f'متغیر {n} تکراری است')
        seen.add(n)
        secret = bool(e.get('secret'))
        default = e.get('default') if e.get('default') is not None else ''
        if not isinstance(default, (str, int, float)) or isinstance(default, bool):
            errs.append(f'مقدار پیش‌فرض {n} باید متن باشد'); default = ''
        if secret and str(default) != '':
            errs.append(f'متغیر محرمانه‌ی {n} نباید مقدار پیش‌فرض داشته باشد (مقدار محرمانه هرگز در قالب ذخیره نمی‌شود)'); default = ''
        t['env'].append({'name': n, 'required': bool(e.get('required')), 'secret': secret, 'default': str(default)[:500],
                         'description': _str(e.get('description'), 'توضیح متغیر', errs, 300, required=False)})
    t['plan'] = data.get('plan') or 'small-g2'
    if t['plan'] not in plans.ORDER:
        errs.append(f'پلن {t["plan"]} نامعتبر است')
    t['bundle'] = data.get('bundle') or 'basic'
    if t['bundle'] not in BUNDLES:
        errs.append('باندل باید basic/standard/pro باشد')
    a = data.get('autoscale') or {}
    if not isinstance(a, dict):
        errs.append('autoscale باید شیء باشد'); a = {}
    t['autoscale'] = {'min_plan': a.get('min_plan') or t['plan'], 'max_plan': a.get('max_plan') or t['plan'],
                      'max_toman_day': _int(a.get('max_toman_day', 60000), 1000, 10_000_000, 'سقف هزینه‌ی روزانه', errs)}
    lo, hi = t['autoscale']['min_plan'], t['autoscale']['max_plan']
    if lo not in plans.ORDER or hi not in plans.ORDER:
        errs.append('min_plan/max_plan نامعتبر است')
    elif plans.ORDER.index(lo) > plans.ORDER.index(hi):
        errs.append('min_plan نباید از max_plan بزرگ‌تر باشد')
    elif t['plan'] in plans.ORDER and not plans.within(t['plan'], lo, hi):
        errs.append('پلن پیشنهادی باید بین min_plan و max_plan باشد')
    d = data.get('disk')
    if d in (None, {}, ''):
        t['disk'] = None
    elif not isinstance(d, dict):
        errs.append('disk باید شیء {name, mountTo, size_gb} باشد'); t['disk'] = None
    else:
        t['disk'] = {'name': _str(d.get('name'), 'نام دیسک', errs, 31, rx=re.compile(r'^[a-z0-9][a-z0-9-]{1,30}$')),
                     'mountTo': _str(d.get('mountTo'), 'مسیر اتصال دیسک', errs, 120, rx=re.compile(r'^/(?!.*\.\.)[\w./-]*$')),
                     'size_gb': _int(d.get('size_gb', 1), 1, 100, 'حجم دیسک (گیگ)', errs)}
    files = data.get('files')
    if not isinstance(files, dict) or not files:
        errs.append('files باید شیء {مسیر: محتوا} و شامل Dockerfile باشد'); files = {}
    t['files'], total = {}, 0
    for p, c in files.items():
        if not isinstance(p, str) or not PATH_RX.match(p):
            errs.append(f'مسیر فایل نامعتبر: {str(p)[:60]}'); continue
        if p == 'liara.json':
            errs.append('liara.json از روی port/disk/health_check/cron ساخته می‌شود؛ آن را در files نگذارید'); continue
        if p.split('/')[-1].startswith('.env'):
            errs.append(f'{p}: فایل‌های .env (احتمال وجود اسرار) مجاز نیستند'); continue
        if not isinstance(c, str):
            errs.append(f'محتوای {p} باید متن باشد'); continue
        if len(c) > MAX_FILE:
            errs.append(f'{p} بزرگ‌تر از ۲۰۰ کیلوبایت است')
        total += len(c)
        t['files'][p] = c
    if files and 'Dockerfile' not in t['files']:
        errs.append('Dockerfile لازم است (پلتفرم docker)')
    if len(t['files']) > MAX_FILES or total > MAX_TOTAL:
        errs.append('حجم یا تعداد فایل‌ها بیش از حد مجاز است (۸۰ فایل / ۱ مگابایت)')
    if errs:
        raise TemplateError(errs)
    return t


def _param_value(name, v, errs):
    v = '' if v is None else str(v)
    if name == 'app_name' and not APP_RX.match(v):
        errs.append('نام برنامه باید با حروف کوچک انگلیسی/عدد/خط تیره و ۳ تا ۳۱ نویسه باشد')
    elif name == 'port' and (not v.isdigit() or not 1 <= int(v) <= 65535):
        errs.append('پورت باید عددی بین ۱ و ۶۵۵۳۵ باشد')
    elif len(v) > 200 or re.search(r'[\x00-\x1f]', v):
        errs.append(f'مقدار پارامتر {name} نامعتبر است (یک خط، حداکثر ۲۰۰ نویسه)')
    return v


# ------------------------------------------------------------------ rendering
def values(t, overrides=None, app=None):
    """Param values: template defaults < explicit overrides; app_name/port/health_path are always present."""
    errs = []
    vals = {p['name']: p['default'] for p in t['params']}
    vals.update(app_name=app or 'my-app', port=str(t['port']), health_path=t['health_path'])
    if overrides is not None and not isinstance(overrides, dict):
        raise TemplateError('params باید شیء {نام: مقدار} باشد')
    for k, v in (overrides or {}).items():
        if k not in vals:
            errs.append(f'پارامتر ناشناخته: {k}'); continue
        vals[k] = _param_value(k, v, errs)
    if app:
        vals['app_name'] = _param_value('app_name', app, errs)
    if errs:
        raise TemplateError(errs)
    return vals


def sub(text, vals):
    return SUB_RX.sub(lambda m: vals.get(m.group(1), m.group(0)), text)


def health_check(t, vals):
    """Liara release body: `command` must be an ARRAY (Docker exec form; a string is rejected with 400) and the durations are
    milliseconds (Liara CLI: 'interval(ms)'). Templates store seconds for readability."""
    hc = t.get('health_check')
    if not hc:
        return None
    cmd = sub(hc['command'], vals)
    out = {'command': cmd if isinstance(cmd, list) else ['CMD-SHELL', cmd]}
    for k in ('interval', 'timeout', 'retries', 'startPeriod'):
        if hc.get(k) is not None:
            out[k] = int(hc[k])  # seconds (Liara validates startPeriod <= 3000, i.e. the unit is seconds, not ms)
    return out


def liara_json(t, vals):
    j = {'platform': 'docker', 'app': vals['app_name'], 'port': int(vals['port'])}
    if t.get('disk'):
        j['disks'] = [{'name': t['disk']['name'], 'mountTo': t['disk']['mountTo']}]
    if t.get('health_check'):
        j['healthCheck'] = health_check(t, vals)
    if t.get('cron'):
        j['cron'] = [sub(c, vals) for c in t['cron']]
    return json.dumps(j, ensure_ascii=False, indent=2) + '\n'


def render(t, overrides=None, app=None):
    vals = values(t, overrides, app)
    out = {p: sub(c, vals) for p, c in t['files'].items()}
    out['liara.json'] = liara_json(t, vals)
    return out


def env_defaults(t, vals):
    return {e['name']: sub(e['default'], vals) for e in t['env'] if not e['secret'] and e['default'] != ''}


def price(t, plan=None, bundle=None):
    h = plans.hourly(plan or t['plan'], bundle or t['bundle'])
    return {'hour': h, 'day': round(h * 24), 'month': round(h * 720)}


# ------------------------------------------------------------------ CRUD
def _row(r):
    return {**json.loads(r['data']), 'id': r['id'], 'builtin': False, 'version': r['version'], 'origin': r['origin'],
            'created_at': r['created_at'], 'updated_at': r['updated_at']}


def get(tid):
    if tid in BUILTINS:
        return json.loads(json.dumps(BUILTINS[tid]))
    _ensure()
    r = store.q('select * from templates where id=?', (tid,), one=True)
    return _row(r) if r else None


def must(tid):
    t = get(tid)
    if not t:
        raise TemplateError(f'قالب «{tid}» پیدا نشد')
    return t


def summary(t):
    return {k: t.get(k) for k in ('id', 'name', 'stack', 'description_fa', 'builtin', 'plan', 'bundle', 'port', 'version', 'origin', 'updated_at', 'disk', 'autoscale')} | {
        'price': price(t), 'env': [e['name'] for e in t['env']], 'secrets': [e['name'] for e in t['env'] if e['secret']], 'files': len(t['files']) + 1}


def list_templates(stack=None):
    _ensure()
    custom = [_row(r) for r in store.q('select * from templates order by updated_at desc')]
    return [summary(t) for t in [*BUILTINS.values(), *custom] if not stack or t['stack'] == stack]


def versions(tid):
    t = must(tid)
    if t['builtin']:
        return [{'version': 1, 'ts': 0, 'actor': 'negahban', 'note': 'قالب پیش‌فرض (فقط‌خواندنی)', 'current': True}]
    rows = store.q('select version, ts, actor, note from template_versions where template_id=? order by version desc', (tid,))
    return [{**r, 'current': r['version'] == t['version']} for r in rows]


def view(tid, overrides=None, app=None):
    t = must(tid)
    files = render(t, overrides, app)
    return {**t, 'rendered': [{'path': p, 'content': c} for p, c in files.items()], 'versions': versions(tid), 'price': price(t),
            'editable': {k: t[k] for k in EDITABLE}}


def _slug(name):
    s = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')[:36]
    return s if len(s) >= 2 else ''


def _new_id(want=None, name=None):
    _ensure()
    for cand in (want, _slug(name)):
        if cand and ID_RX.match(cand):
            base, i = cand, 2
            while cand in BUILTINS or store.q('select 1 from templates where id=?', (cand,), one=True):
                cand = f'{base}-{i}'; i += 1
            return cand
    return store.uid('tpl_')


def _save_version(tid, version, data, actor, note):
    store.x('insert into template_versions(template_id,version,data,ts,actor,note) values(?,?,?,?,?,?)', (tid, version, store.J(data), time.time(), actor, note))


def create(data, actor='owner', origin='manual', note='ساخت قالب'):
    t = validate(data)
    tid = _new_id(data.get('id'), t['name'])
    now = time.time()
    store.x('insert into templates(id,name,stack,version,data,origin,created_at,updated_at) values(?,?,?,?,?,?,?,?)',
            (tid, t['name'], t['stack'], 1, store.J(t), origin, now, now))
    _save_version(tid, 1, t, actor, note)
    store.audit(actor, 'template_create', None, {'template': tid, 'name': t['name'], 'origin': origin})
    return get(tid)


def update(tid, data, actor='owner', note='ویرایش'):
    cur = must(tid)
    if cur['builtin']:
        raise TemplateError('قالب‌های پیش‌فرض فقط‌خواندنی‌اند؛ ابتدا از آن یک کپی بسازید')
    t = validate({**{k: cur[k] for k in EDITABLE}, **{k: v for k, v in data.items() if k in EDITABLE}})
    if t == {k: cur[k] for k in EDITABLE}:
        return cur
    v = cur['version'] + 1
    store.x('update templates set name=?, stack=?, version=?, data=?, updated_at=? where id=?', (t['name'], t['stack'], v, store.J(t), time.time(), tid))
    _save_version(tid, v, t, actor, (note or 'ویرایش')[:200])
    store.audit(actor, 'template_update', None, {'template': tid, 'version': v, 'note': note})
    return get(tid)


def restore(tid, version, actor='owner'):
    cur = must(tid)
    if cur['builtin']:
        raise TemplateError('قالب‌های پیش‌فرض نسخه‌ی دیگری ندارند')
    r = store.q('select data from template_versions where template_id=? and version=?', (tid, int(version)), one=True)
    if not r:
        raise TemplateError(f'نسخه‌ی {version} پیدا نشد')
    t = update(tid, json.loads(r['data']), actor, f'بازگردانی نسخه‌ی {version}')
    store.audit(actor, 'template_restore', None, {'template': tid, 'restored': int(version), 'version': t['version']})
    return t


def clone(tid, name=None, actor='owner', overrides=None):
    src = must(tid)
    data = {**{k: src[k] for k in EDITABLE}, 'name': (name or f"{src['name']} (کپی)")[:80], **(overrides or {})}
    t = create(data, actor, origin=f'clone:{tid}', note=f'کپی از {tid} نسخه‌ی {src["version"]}')
    store.audit(actor, 'template_clone', None, {'from': tid, 'template': t['id']})
    return t


def delete(tid, confirm_name, actor='owner'):
    t = must(tid)
    if t['builtin']:
        raise TemplateError('قالب‌های پیش‌فرض قابل حذف نیستند')
    if (confirm_name or '').strip() != t['name']:
        raise TemplateError('برای حذف، نام قالب را دقیقاً تایپ کنید')
    store.x('delete from templates where id=?', (tid,))
    store.x('delete from template_versions where template_id=?', (tid,))
    store.audit(actor, 'template_delete', None, {'template': tid, 'name': t['name'], 'versions': t['version']})
    return {'deleted': tid}


# ------------------------------------------------------------------ auto-save from the new-project wizard
TEXT_EXT = ('.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.json', '.txt', '.toml', '.cfg', '.ini', '.yaml', '.yml', '.md', '.html', '.htm', '.css',
            '.scss', '.php', '.go', '.mod', '.sum', '.conf', '.sh', '.lock', '.xml', '.svg', '.vue', '.svelte', '.sql', '.rb', '.java', '.kt', '.cs', '.csproj')
TEXT_NAMES = ('Dockerfile', 'Procfile', 'Makefile', 'Gemfile', '.dockerignore', '.gitignore', '.htaccess', '.nvmrc', '.python-version', 'artisan', 'manage.py')
SECRET_FILES = re.compile(r'(?i)(^\.env|\.pem$|\.key$|\.p12$|\.pfx$|^id_(rsa|ed25519|ecdsa)|^\.npmrc$|^\.pypirc$|^\.netrc$|credentials?\.json$|service[-_]account.*\.json$|secrets?\.(json|ya?ml|toml)$)')
SKIP_PARTS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache', '.next', 'dist', 'build', 'out', 'vendor', '.negahban', 'staticfiles'}
_SCRUB = [(re.compile(r'(?i)(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://([^:/\s@]+):([^@/\s]+)@'), r'\1://\2:***@'),
          (re.compile(r'\bsk-[A-Za-z0-9_\-]{12,}'), 'sk-***'), (re.compile(r'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}'), '***jwt***'),
          (re.compile(r'\b(ghp|gho|github_pat|glpat|xox[bap])[-_][A-Za-z0-9_\-]{16,}'), '***'), (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '***')]
# platform -> builtin whose Dockerfile/health probe fits a project that has no Dockerfile
BORROW = {'node': 'node-express', 'next': 'node-express', 'django': 'django-postgres', 'laravel': 'laravel', 'php': 'laravel', 'go': 'go', 'static': 'static-site'}
STACK = {'node': 'node', 'next': 'nextjs', 'django': 'django', 'laravel': 'php', 'php': 'php', 'go': 'go', 'static': 'static', 'flask': 'flask', 'react': 'static', 'vue': 'static'}


def _scrub(text, known_values=()):
    for v in [*settings.secrets(), *known_values]:
        if v and len(v) >= 6:
            text = text.replace(v, '***')
    for rx, rep in _SCRUB:
        text = rx.sub(rep, text)
    return text


def _param_port(dockerfile, port):
    """EXPOSE/CMD/ENTRYPOINT/ENV lines: the literal port becomes {{port}}."""
    p = re.escape(str(port))
    return re.sub(r'(?im)^(\s*(?:EXPOSE|CMD|ENTRYPOINT|ENV|ARG)\b.*)$', lambda m: re.sub(rf'(?<![\w.]){p}(?![\w.])', '{{port}}', m.group(1)), dockerfile)


def save_from_project(project_id, app, analysis=None, actor='negahban'):
    """Turn a freshly deployed wizard project into a reusable template: parameterised (app_name/port), secrets stripped (env var
    NAMES only; .env files, keys and known values are never copied). Re-deploying the same project adds a version instead of a copy.
    Returns the template, or None when the project cannot be expressed as a docker template."""
    from .analyzer import analyze_dir, project_dir
    d = project_dir(project_id)
    if not d.is_dir():
        return None
    a = analysis or analyze_dir(d)
    meta = store.kv_get(f'project:{project_id}', {}) or {}
    known = [str(v) for v in (meta.get('envs') or {}).values()]
    port = int(a.get('port') or meta.get('port') or 8080)
    files, total, skipped = {}, 0, 0
    for p in sorted(d.rglob('*')):
        rel = p.relative_to(d)
        if not p.is_file() or p.is_symlink() or set(rel.parts) & SKIP_PARTS:
            continue
        r = rel.as_posix()
        if SECRET_FILES.search(p.name) or r == 'liara.json' or not PATH_RX.match(r) or not (p.suffix.lower() in TEXT_EXT or p.name in TEXT_NAMES):
            skipped += 1; continue
        try:
            txt = p.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            skipped += 1; continue
        if len(txt) > MAX_FILE or total + len(txt) > MAX_TOTAL - 50_000 or len(files) >= MAX_FILES - 3:
            skipped += 1; continue
        files[r] = _scrub(txt, known)
        total += len(txt)
    platform = a.get('platform') or 'docker'
    hc, params, borrowed = None, [], None
    if 'Dockerfile' in files:
        files['Dockerfile'] = _param_port(files['Dockerfile'], port)
    elif platform in BORROW:
        b = BUILTINS[BORROW[platform]]
        borrowed = b['id']
        files['Dockerfile'] = b['files']['Dockerfile']
        for extra in ('healthcheck.py', 'healthcheck.js', 'healthcheck.php', 'nginx.conf'):
            if extra in b['files'] and extra not in files:
                files[extra] = b['files'][extra]
        params = [dict(p) for p in b['params']]
        if borrowed == 'django-postgres':
            wsgi = next((x[:-3].replace('/', '.') for x in sorted(files) if x.endswith('/wsgi.py') and x.count('/') == 1), None)
            params = [{**params[0], 'default': wsgi or params[0]['default']}]
        if b['health_check'] and not b['health_check']['command'].startswith('wget'):
            hc = b['health_check']
        port = b['port'] if platform in ('static', 'laravel', 'php') else port
    else:
        return None
    for name in ('package.json', 'go.mod', 'composer.json'):
        if name in files:
            files[name] = files[name].replace(app, '{{app_name}}')
    plan = a.get('recommended_plan') if a.get('recommended_plan') in plans.ORDER else 'small-g2'
    hi = plans.neighbour(plan, 1)
    stack = 'fastapi' if a.get('framework') == 'FastAPI' else STACK.get(platform, 'docker')
    data = {'name': f"{app} ({a.get('framework') or platform})"[:80], 'stack': stack, 'port': port, 'health_path': meta.get('health_path') or '/',
            'description_fa': f"ذخیره‌ی خودکار از پروژه‌ی {project_id} که روی برنامه‌ی {app} مستقر شد" + (f'؛ Dockerfile از قالب {borrowed}' if borrowed else '')
                              + (f'؛ {skipped} فایل غیرمتنی/حساس/بزرگ کپی نشد' if skipped else '') + '. مقادیر متغیرها ذخیره نشده‌اند.',
            'health_check': hc, 'params': params, 'plan': plan, 'bundle': 'basic',
            'env': [{'name': n, 'required': False, 'secret': bool(SECRET_NAME.search(n)), 'default': '', 'description': 'از کد پروژه تشخیص داده شد'}
                    for n in (a.get('env_vars') or []) if ENV_RX.match(n)][:60],
            'autoscale': {'min_plan': plan, 'max_plan': hi, 'max_toman_day': int(math.ceil(plans.hourly(hi, 'basic') * 24 * 1.2 / 1000) * 1000)},
            'disk': None, 'files': files}
    _ensure()
    ex = store.q('select id from templates where origin=?', (f'project:{project_id}',), one=True)
    if ex:
        return update(ex['id'], data, actor, f'استقرار دوباره روی {app}')
    return create({**data, 'id': f'{app}-tpl'}, actor, origin=f'project:{project_id}', note=f'ذخیره‌ی خودکار پس از استقرار {app}')


def _load_builtins():
    import logging
    for m in _BUILTIN_META:
        try:
            BUILTINS[m['id']] = _builtin(m)
        except (OSError, TemplateError) as e:  # a broken seed must never take the whole app (tools registry) down
            logging.getLogger('negahban.templates').error('builtin template %s skipped: %s', m['id'], e)


_load_builtins()
