"""Custom-domain wizard: add the domain to the Liara app via API, show the exact records the owner must create at their
DNS provider / IRNIC, poll verification, then provision SSL. Liara cannot edit DNS hosted elsewhere (e.g. ArvanCloud,
nic.ir) — those steps are the owner's, and the wizard says so."""
from .liara import LiaraError, liara


def records_for(d):
    name = d.get('name', '')
    labels = name.split('.')
    root = len(labels) == 2 or (len(labels) == 3 and labels[-2] in ('co', 'ac', 'org', 'gov', 'id', 'net') and labels[-1] == 'ir')
    sub = '' if root else '.'.join(labels[:-2])
    target = d.get('target') or d.get('CNameRecord') or 'از پنل لیارا'
    rec = [
        {'type': 'ALIAS' if root else 'CNAME', 'name': '@' if root else sub, 'value': target,
         'why': 'هدایت ترافیک دامنه به برنامه در لیارا', 'note': 'اگر سرویس DNS شما ALIAS ندارد، از ANAME/CNAME flattening استفاده کنید' if root else ''},
        {'type': 'TXT', 'name': 'liara-challenge' if root else f'liara-challenge.{sub}', 'value': d.get('verificationCode') or '(کد تأیید از لیارا)',
         'why': 'اثبات مالکیت دامنه'},
        {'type': 'CNAME', 'name': f'_acme-challenge' + ('' if root else f'.{sub}'), 'value': f"{name.replace('.', '-')}._acme-challenge.liara.cloud",
         'why': 'صدور خودکار گواهی SSL (پروکسی CDN را برای این رکورد خاموش کنید)'},
    ]
    return rec


async def add(app, domain):
    r = await liara.add_domain(domain, app)
    did = ((r or {}).get('domain') or {}).get('_id')
    return {'domain_id': did, **(await guide(did) if did else {})}


async def guide(domain_id):
    d = await liara.domain(domain_id)
    try:
        chk = await liara.check_domain(domain_id)
        d.update({k: v for k, v in chk.items() if k in ('status', 'certificatesStatus')})
    except LiaraError:
        pass
    steps = [
        'وارد پنل سرویس DNS دامنه شوید (مثلاً آروان‌کلاد، کلادفلر یا هر جایی که nameserverهای دامنه به آن اشاره می‌کنند).',
        'سه رکورد جدول زیر را دقیقاً همان‌طور اضافه کنید.',
        'اگر دامنه‌ی .ir دارید و می‌خواهید DNS را به لیارا بسپارید: در nic.ir بخش «تنظیمات DNS» nameserverها را به مقادیری که نگهبان پس از ساخت ناحیه نشان می‌دهد تغییر دهید (این کار فقط از سایت ایرنیک ممکن است).',
        'پس از ثبت رکوردها، دکمه‌ی «بررسی دوباره» را بزنید؛ انتشار DNS ممکن است تا چند ساعت طول بکشد.',
        'وقتی وضعیت «فعال» شد، نگهبان (با تأیید شما) گواهی SSL رایگان را درخواست می‌کند.',
    ]
    return {'domain': {k: d.get(k) for k in ('_id', 'name', 'status', 'certificatesStatus', 'target', 'CNameRecord')},
            'records': records_for(d), 'steps': steps,
            'api_limit': 'لیارا فقط DNS میزبانی‌شده روی خودش را از طریق API ویرایش می‌کند؛ رکوردهای ایرنیک/آروان/کلادفلر را باید خودتان ثبت کنید.'}
