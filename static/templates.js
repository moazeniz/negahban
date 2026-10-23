'use strict';
/* Templates library (قالب‌های استقرار): list, view with highlighted files, form + JSON editor, versions, clone/delete and
   "deploy from template" which creates an approval card. CSP-safe: no inline handlers, every dynamic string goes through esc(). */
(() => {
  const N = window.Ngh; if (!N) return;
  const {api, esc, fa, toman, toast, dt} = N;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  N.TITLES.templates = 'قالب‌های استقرار';
  const STACK_FA = {fastapi: 'FastAPI', node: 'Node.js', nextjs: 'Next.js', django: 'Django', php: 'PHP/Laravel', go: 'Go', static: 'ایستا', worker: 'Worker', docker: 'Docker', flask: 'Flask'};
  const BUNDLE_FA = {basic: 'برنزی (basic)', standard: 'نقره‌ای (standard)', pro: 'طلایی (pro)'};
  const T = {plans: [], cur: null};

  if (!$('#tplCss')) {
    const st = document.createElement('style'); st.id = 'tplCss';
    st.textContent = `.tpl-card{cursor:pointer;display:grid;gap:8px;align-content:start}.tpl-card:hover{border-color:#b9c4cf}.tpl-card p{margin:0}
      .tpl-code .k{color:#93c5fd}.tpl-code .s{color:#86efac}.tpl-code .c{color:#94a3b8;font-style:italic}.tpl-code .w{color:#f0abfc;font-weight:700}
      .tpl-code .n{color:#fcd34d}.tpl-code .p{color:#fb923c;font-weight:700}.tpl-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
      .tpl-tabs .btn.on{border-color:var(--brand);color:var(--brand);font-weight:700}textarea.tpl-json{min-height:420px;font-family:var(--mono);font-size:12.5px;direction:ltr;text-align:left;white-space:pre}
      .tpl-errs{margin:6px 0 0;padding-inline-start:18px;color:var(--bad);font-size:13px}.tpl-table{width:100%;border-collapse:collapse;font-size:13px}
      .tpl-table th,.tpl-table td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:start;vertical-align:top}.tpl-secrets{border-inline-start:5px solid var(--warn)}
      #tplProp .prop{grid-template-columns:minmax(0,1fr);min-width:0}#tplProp .prop>*{min-width:0}#tplProp .diff{max-width:100%}`;
    document.head.appendChild(st);
  }

  /* ------------------------------------------------------------ helpers */
  async function busy(btn, text, fn) {
    if (!btn || btn.disabled) return null;
    const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="spin"></span> ${esc(text)}`;
    try { return await fn(); }
    catch (e) { toast(e.message, 6500); return null; }
    finally { if (btn.isConnected) { btn.disabled = false; btn.innerHTML = old; } }
  }
  const errList = msg => `<ul class="tpl-errs">${String(msg || '').split('\n').filter(Boolean).map(l => `<li>${esc(l)}</li>`).join('')}</ul>`;
  const planName = id => { const p = T.plans.find(x => x.id === id); return p ? `${p.name_fa} (${p.id})` : id; };
  const closeModal = () => $('#modal').classList.add('hidden');
  async function loadPlans() { if (!T.plans.length) { const r = await api('/api/templates'); T.plans = r.plans; } return T.plans; }

  /* tiny, safe highlighter: tokens are matched on the RAW text and every piece is escaped before it is wrapped */
  function hl(code, path) {
    if (code.length > 80000) return esc(code);
    const name = path.split('/').pop(), ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
    const hash = name === 'Dockerfile' || ['py', 'sh', 'yml', 'yaml', 'toml', 'conf', 'txt', 'htaccess', 'mod'].includes(ext);
    const slash = ['js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'go', 'php', 'css', 'scss', 'java', 'cs'].includes(ext);
    const kw = name === 'Dockerfile' ? String.raw`^\s*(?:FROM|RUN|CMD|COPY|ADD|WORKDIR|ENV|EXPOSE|ARG|USER|ENTRYPOINT|LABEL|HEALTHCHECK|VOLUME|SHELL)\b`
      : String.raw`\b(?:import|from|as|def|class|return|if|else|elif|for|while|try|except|with|const|let|var|function|package|func|require|module|exports?|default|server|location|listen|root|index|async|await|new|echo|exit|true|false|True|False|None|null)\b`;
    const rx = new RegExp([String.raw`("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|\`(?:[^\`\\]|\\.)*\`)`,
      hash ? String.raw`(#[^\n]*)` : slash ? String.raw`(\/\/[^\n]*|\/\*[\s\S]*?\*\/)` : '(\\b\\B)',
      String.raw`(\{\{\s*[a-z_][a-z0-9_]*\s*\}\})`, String.raw`(\b\d+(?:\.\d+)?\b)`, `(${kw})`].join('|'), 'gm');
    let out = '', last = 0, m;
    while ((m = rx.exec(code))) {
      if (!m[0]) { rx.lastIndex++; continue; }
      const cls = m[1] ? (/^\s*:/.test(code.substr(rx.lastIndex, 12)) ? 'k' : 's') : m[2] ? 'c' : m[3] ? 'p' : m[4] ? 'n' : 'w';
      out += esc(code.slice(last, m.index)) + `<span class="${cls}">${esc(m[0])}</span>`; last = rx.lastIndex;
    }
    return out + esc(code.slice(last));
  }

  /* ------------------------------------------------------------ list */
  function card(t) {
    const p = t.price || {};
    return `<div class="card tpl-card" data-tpl="${esc(t.id)}" tabindex="0">
      <div class="row"><b>${esc(t.name)}</b><span class="chip brand right">${esc(STACK_FA[t.stack] || t.stack)}</span></div>
      <div class="row small">${t.builtin ? '<span class="chip info">پیش‌فرض · فقط‌خواندنی</span>' : `<span class="chip">نسخه‌ی ${fa(t.version)}</span>`}
        ${t.origin && t.origin.startsWith('project:') ? '<span class="chip ok">ذخیره‌ی خودکار</span>' : ''}${t.disk ? `<span class="chip">💾 ${esc(t.disk.name)}</span>` : ''}
        ${(t.secrets || []).length ? `<span class="chip warn">🔐 ${fa(t.secrets.length)} متغیر محرمانه</span>` : ''}</div>
      <p class="muted small">${esc((t.description_fa || '').slice(0, 180))}</p>
      <div class="facts"><div class="fact"><small>پلن پیشنهادی</small><span class="ltr">${esc(t.plan)}</span> · ${esc(t.bundle)}</div>
        <div class="fact"><small>هزینه</small>${toman(p.hour)} در ساعت<br><span class="muted">≈ ${toman(p.month)} در ماه</span></div></div>
      <div class="row"><button class="btn sm primary" data-tact="deploy">🚀 استقرار</button><button class="btn sm" data-tact="open">مشاهده</button>
        <button class="btn sm" data-tact="clone">کپی</button></div></div>`;
  }
  async function list() {
    const v = $('#view'); v.innerHTML = '<div class="empty"><span class="spin"></span> در حال بارگذاری قالب‌ها…</div>';
    let r; try { r = await api('/api/templates'); } catch (e) { v.innerHTML = `<div class="card err">${esc(e.message)}</div>`; return; }
    T.plans = r.plans;
    const b = r.templates.filter(t => t.builtin), c = r.templates.filter(t => !t.builtin);
    v.innerHTML = `<div class="card small muted" style="margin-bottom:12px">قالب‌های آماده برای پلتفرم docker لیارا: Dockerfile، liara.json (پورت، دیسک، healthCheck، cron)، سورس اولیه،
      فهرست متغیرهای محیطی، پلن پیشنهادی و سقف بودجه‌ی خودران. نگهبان به جای نوشتن دوباره‌ی تنظیمات، از همین قالب‌ها استفاده می‌کند و هر پروژه‌ی تازه‌ای که مستقر کند را هم (بدون مقادیر محرمانه) اینجا ذخیره می‌کند.</div>
      <div class="sec-title"><h2>قالب‌های شما (${fa(c.length)})</h2><span class="grow"></span><button class="btn sm primary" id="tplNew">➕ قالب جدید</button></div>
      <div class="grid g3">${c.map(card).join('') || '<div class="empty card">هنوز قالبی نساخته‌اید؛ از یک قالب پیش‌فرض کپی بگیرید یا یک پروژه را با ویزارد مستقر کنید.</div>'}</div>
      <div class="sec-title"><h2>قالب‌های پیش‌فرض (${fa(b.length)})</h2></div><div class="grid g3">${b.map(card).join('')}</div>`;
    $$('.tpl-card', v).forEach(el => {
      const id = el.dataset.tpl;
      el.addEventListener('click', e => {
        const act = e.target.closest("[data-tact]"); const a = act ? act.dataset.tact : 'open';
        if (a === 'clone') cloneDialog(id, el.querySelector('b').textContent); else N.go('templates', a === 'deploy' ? id + ':deploy' : id);
      });
      el.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target === el) N.go('templates', id); });
    });
    $('#tplNew').onclick = e => busy(e.currentTarget, 'در حال آماده‌سازی…', async () => {
      const base = (await api('/api/templates/docker-generic')).editable;
      editor({...base, name: 'قالب جدید من'}, null);
    });
  }

  /* ------------------------------------------------------------ view */
  async function viewOne(arg) {
    const [id, mode] = String(arg).split(':');
    const v = $('#view'); v.innerHTML = '<div class="empty"><span class="spin"></span> در حال بارگذاری…</div>';
    let t; try { [t] = await Promise.all([api('/api/templates/' + encodeURIComponent(id)), loadPlans()]); }
    catch (e) { v.innerHTML = `<div class="card err">${esc(e.message)}</div><button class="btn" id="tplBack">→ همه‌ی قالب‌ها</button>`; $('#tplBack').onclick = () => N.go('templates'); return; }
    T.cur = t;
    const hc = t.health_check, a = t.autoscale || {}, files = t.rendered;
    v.innerHTML = `<div class="row" style="margin-bottom:10px"><button class="btn sm" id="tplBack">→ همه‌ی قالب‌ها</button></div>
    <div class="card">
      <div class="row"><h2 style="margin:0">${esc(t.name)}</h2><span class="chip brand">${esc(STACK_FA[t.stack] || t.stack)}</span>
        ${t.builtin ? '<span class="chip info">پیش‌فرض · فقط‌خواندنی</span>' : `<span class="chip">نسخه‌ی ${fa(t.version)}</span><span class="chip ltr">${esc(t.id)}</span>`}</div>
      <p>${esc(t.description_fa || '')}</p>
      <div class="facts">
        <div class="fact"><small>پلن پیشنهادی</small>${esc(planName(t.plan))} · ${esc(BUNDLE_FA[t.bundle] || t.bundle)}</div>
        <div class="fact"><small>هزینه</small>${toman(t.price.hour)} در ساعت · ≈ ${toman(t.price.month)} در ماه</div>
        <div class="fact"><small>پورت / مسیر سلامت</small><span class="ltr">${esc(t.port)} ${esc(t.health_path)}</span></div>
        <div class="fact"><small>healthCheck</small>${hc ? `<code>${esc(hc.command)}</code><br><span class="muted">هر ${fa(hc.interval)} ثانیه، ${fa(hc.retries)} تلاش</span>` : 'ندارد'}</div>
        <div class="fact"><small>دیسک</small>${t.disk ? `<span class="ltr">${esc(t.disk.name)} → ${esc(t.disk.mountTo)}</span> (${fa(t.disk.size_gb)} گیگ)` : 'ندارد'}</div>
        <div class="fact"><small>cron</small>${(t.cron || []).length ? t.cron.map(c => `<code>${esc(c)}</code>`).join('<br>') : 'ندارد'}</div>
      </div>
      <div class="row" style="margin-top:12px"><button class="btn primary" id="tplDeploy">🚀 استقرار از این قالب</button><button class="btn" id="tplClone">📄 کپی</button>
        ${t.builtin ? '<span class="muted small">برای تغییر، ابتدا از آن کپی بسازید.</span>' : '<button class="btn" id="tplEdit">✏️ ویرایش</button><button class="btn danger" id="tplDel">🗑 حذف</button>'}</div>
    </div>
    <div id="tplDeployBox"></div>
    <div class="grid g2" style="margin-top:16px">
      <div class="card"><h3>متغیرهای محیطی</h3>${envTable(t.env)}</div>
      <div class="card"><h3>خودران و بودجه</h3><div class="facts">
        <div class="fact"><small>کمینه‌ی پلن</small>${esc(planName(a.min_plan))}</div><div class="fact"><small>بیشینه‌ی پلن</small>${esc(planName(a.max_plan))}</div>
        <div class="fact"><small>سقف هزینه‌ی روزانه</small>${toman(a.max_toman_day)}</div></div>
        <p class="muted small">پس از استقرار، همین محدوده روی سیاست خودران برنامه تنظیم می‌شود (تغییر خودکار پلن فقط داخل این بازه و زیر این سقف).</p>
        <h3>پارامترها</h3><div class="tablewrap"><table class="tpl-table"><tr><th>نام</th><th>پیش‌فرض</th><th>توضیح</th></tr>
        ${[{name: 'app_name', default: '(نام برنامه)', description: 'نام برنامه در لیارا'}, {name: 'port', default: String(t.port), description: 'پورت شنود'},
           {name: 'health_path', default: t.health_path, description: 'مسیر سلامت'}, ...t.params].map(p => `<tr><td><code>{{${esc(p.name)}}}</code></td><td class="ltr">${esc(p.default)}</td><td>${esc(p.description || '')}</td></tr>`).join('')}</table></div></div>
    </div>
    <div class="card" style="margin-top:16px"><h3>فایل‌ها (${fa(files.length)})</h3><div class="tpl-tabs">${files.map((f, i) => `<button class="btn sm${i ? '' : ' on'}" data-file="${i}"><span class="ltr">${esc(f.path)}</span></button>`).join('')}</div>
      <div class="diff tpl-code" id="tplCode"></div><div class="muted small" style="margin-top:6px">مقادیر {{param}} با نام برنامه/پورت هنگام استقرار جایگزین می‌شوند. liara.json از تنظیمات قالب ساخته می‌شود.</div></div>
    <div class="card" style="margin-top:16px"><h3>تاریخچه‌ی نسخه‌ها</h3><div class="tablewrap"><table class="tpl-table"><tr><th>نسخه</th><th>زمان</th><th>توسط</th><th>یادداشت</th><th></th></tr>
      ${t.versions.map(x => `<tr><td>${fa(x.version)} ${x.current ? '<span class="chip ok">فعلی</span>' : ''}</td><td>${x.ts ? esc(dt(x.ts)) : '—'}</td><td>${esc(x.actor)}</td><td>${esc(x.note || '')}</td>
        <td>${!x.current && !t.builtin ? `<button class="btn sm" data-restore="${esc(x.version)}">↺ بازگردانی</button>` : ''}</td></tr>`).join('')}</table></div></div>`;
    const showFile = i => { $('#tplCode').innerHTML = hl(files[i].content, files[i].path); $$('[data-file]').forEach(b => b.classList.toggle('on', +b.dataset.file === i)); };
    $$('[data-file]').forEach(b => b.onclick = () => showFile(+b.dataset.file)); showFile(Math.max(0, files.findIndex(f => f.path === 'Dockerfile')));
    $('#tplBack').onclick = () => N.go('templates');
    $('#tplDeploy').onclick = () => deployForm(t);
    $('#tplClone').onclick = () => cloneDialog(t.id, t.name);
    if (!t.builtin) {
      $('#tplEdit').onclick = () => editor(t.editable, t);
      $('#tplDel').onclick = () => deleteDialog(t);
    }
    $$('[data-restore]').forEach(b => b.onclick = () => {
      if (!confirm(`محتوای نسخه‌ی ${b.dataset.restore} به‌عنوان نسخه‌ی جدید ذخیره شود؟`)) return;
      busy(b, 'در حال بازگردانی…', async () => { const r = await api(`/api/templates/${encodeURIComponent(t.id)}/restore/${encodeURIComponent(b.dataset.restore)}`, {method: 'POST'});
        toast(`نسخه‌ی ${b.dataset.restore} بازگردانی شد (نسخه‌ی جدید: ${r.version})`); viewOne(t.id); });
    });
    if (mode === 'deploy') deployForm(t);
  }
  function envTable(env) {
    if (!env.length) return '<div class="muted small">متغیری تعریف نشده</div>';
    return `<div class="tablewrap"><table class="tpl-table"><tr><th>نام</th><th>پیش‌فرض</th><th>نوع</th><th>توضیح</th></tr>${env.map(e => `<tr><td><code>${esc(e.name)}</code></td>
      <td class="ltr">${e.secret ? '<span class="muted">—</span>' : esc(e.default || '—')}</td>
      <td>${e.secret ? '<span class="chip warn">🔐 محرمانه</span>' : ''}${e.required ? '<span class="chip bad">لازم</span>' : '<span class="chip">اختیاری</span>'}</td>
      <td class="small">${esc(e.description || '')}</td></tr>`).join('')}</table></div><p class="muted small">مقدار متغیرهای محرمانه هرگز در قالب ذخیره نمی‌شود؛ روی کارت تأیید یا بعداً وارد می‌شود.</p>`;
  }

  /* ------------------------------------------------------------ deploy */
  function deployForm(t) {
    const box = $('#tplDeployBox'); if (!box) return;
    const plain = t.env.filter(e => !e.secret), secrets = t.env.filter(e => e.secret);
    box.innerHTML = `<div class="card" style="margin-top:16px"><h3>🚀 استقرار از «${esc(t.name)}»</h3>
      <form class="form" id="tplDeployForm" autocomplete="off">
        <div class="grid g2">
          <label>نام برنامه در لیارا (جدید یا موجود)<input name="app" class="ltr" required pattern="[a-z0-9][a-z0-9\\-]{2,30}" placeholder="my-app" maxlength="31"></label>
          <label>پلن<select name="plan">${T.plans.map(p => `<option value="${esc(p.id)}"${p.id === t.plan ? ' selected' : ''}>${esc(p.name_fa)} — ${esc(p.cpu)} هسته / ${esc(p.ram)} گیگ</option>`).join('')}</select></label>
          <label>باندل<select name="bundle">${Object.entries(BUNDLE_FA).map(([k, l]) => `<option value="${k}"${k === t.bundle ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
          <label>پورت<input name="p_port" class="ltr" inputmode="numeric" value="${esc(t.port)}"></label>
          ${t.params.map(p => `<label>${esc(p.description || p.name)} <code>${esc(p.name)}</code><input name="p_${esc(p.name)}" class="ltr" value="${esc(p.default)}"></label>`).join('')}
        </div>
        <div class="fact" id="tplCost"></div>
        ${plain.length ? `<h3 style="margin:4px 0 0">متغیرهای محیطی (غیرمحرمانه)</h3><div class="grid g2">${plain.map(e => `<label><span><code>${esc(e.name)}</code> ${e.required ? '<span class="chip bad">لازم</span>' : ''}</span>
          <input data-env="${esc(e.name)}" class="ltr" value="${esc(e.default)}" placeholder="${esc(e.description || '')}"><small class="muted">${esc(e.description || '')}</small></label>`).join('')}</div>` : ''}
        ${secrets.length ? `<div class="muted small">🔐 متغیرهای محرمانه (${secrets.map(e => esc(e.name)).join('، ')}) در این فرم ارسال نمی‌شوند؛ پس از ساخت کارت، مقدارشان را کنار همان کارت وارد کنید یا بعداً با «تنظیم متغیر» بدهید.</div>` : ''}
        <div class="row"><button class="btn primary" type="submit">ساخت کارت تأیید</button><span class="muted small">هیچ تغییری پیش از تأیید شما روی لیارا اجرا نمی‌شود.</span></div>
      </form><div id="tplProp" style="margin-top:12px"></div></div>`;
    const f = $('#tplDeployForm');
    const cost = () => { const p = T.plans.find(x => x.id === f.plan.value) || {}; const h = (p.bundles || {})[f.bundle.value] ?? p.hour;
      $('#tplCost').innerHTML = `<small>هزینه‌ی تقریبی</small>${toman(h)} در ساعت · ≈ ${toman(h * 24)} در روز · ≈ ${toman(h * 720)} در ماه`; };
    f.plan.onchange = cost; f.bundle.onchange = cost; cost();
    box.scrollIntoView({behavior: 'smooth', block: 'start'}); f.app.focus();
    f.onsubmit = e => {
      e.preventDefault();
      if (!f.app.checkValidity()) { toast('نام برنامه: حروف کوچک انگلیسی، عدد و خط تیره (۳ تا ۳۱ نویسه)'); f.app.focus(); return; }
      const envs = {}, params = {};
      $$('[data-env]', f).forEach(i => { const d = (t.env.find(x => x.name === i.dataset.env) || {}).default || ''; if (i.value !== '' && i.value !== d) envs[i.dataset.env] = i.value; });
      if (f.p_port.value.trim() !== String(t.port)) params.port = f.p_port.value.trim();
      t.params.forEach(p => { const val = f['p_' + p.name].value; if (val !== p.default) params[p.name] = val; });
      const body = {app: f.app.value.trim(), plan: f.plan.value, bundle: f.bundle.value, envs, params};
      busy(f.querySelector('[type=submit]'), 'در حال ساخت کارت…', async () => {
        const r = await api(`/api/templates/${encodeURIComponent(t.id)}/deploy`, {method: 'POST', body});
        const id = r.proposal.id;
        const p = (await api('/api/proposals')).find(x => x.id === id) || {...r.proposal, status: 'pending', args: {app: body.app}, created_at: Date.now() / 1000};
        showProposal(p, secrets);
        toast('کارت تأیید ساخته شد؛ پس از بررسی، «تأیید و اجرا» را بزنید');
      });
    };
  }
  function showProposal(p, secrets) {
    const box = $('#tplProp'); if (!box) return;
    box.innerHTML = (secrets.length && p.status === 'pending' ? `<div class="card tpl-secrets" style="margin-bottom:10px"><h3>🔐 مقادیر محرمانه برای این استقرار</h3>
      <div class="grid g2">${secrets.map(e => `<label><span><code>${esc(e.name)}</code> ${e.required ? '<span class="chip bad">لازم</span>' : ''}</span>
        <input type="password" autocomplete="new-password" class="ltr" data-tplsecret="${esc(e.name)}" placeholder="${esc(e.description || '')}"></label>`).join('')}</div>
      <p class="muted small">این مقادیر فقط هنگام زدن «تأیید و اجرا» ارسال می‌شوند، در کارت/گزارش‌ها نمایش داده نمی‌شوند و پس از استقرار از حافظه‌ی نگهبان پاک می‌شوند.</p></div>` : '')
      + N.propCard(p);
    N.bindProps();
    box._tpl = {id: p.id, secrets, sent: false};
    if (box._tplBound) return;
    box._tplBound = true;
    /* capture phase: send the typed secrets first, then let app.js' own approve handler run (it is re-bound on live updates) */
    box.addEventListener('click', ev => {
      const st = box._tpl, b = st && ev.target.closest(`[data-approve="${st.id}"]`);
      if (!b || st.sent) return;
      const values = {}; $$('[data-tplsecret]', box).forEach(i => { if (i.value) values[i.dataset.tplsecret] = i.value; });
      const miss = st.secrets.filter(e => e.required && !values[e.name]).map(e => e.name);
      if (!Object.keys(values).length && !miss.length) { st.sent = true; return; }
      ev.stopPropagation(); ev.preventDefault();
      if (miss.length && !confirm(`متغیرهای لازم ${miss.join('، ')} مقدار ندارند و برنامه ممکن است بالا نیاید (بعداً با «تنظیم متغیر» قابل افزودن است). ادامه می‌دهید؟`)) return;
      busy(b, 'ارسال مقادیر محرمانه…', async () => {
        if (Object.keys(values).length) await api(`/api/templates/proposals/${encodeURIComponent(st.id)}/secrets`, {method: 'POST', body: {values}});
        st.sent = true; $$('[data-tplsecret]', box).forEach(i => { i.value = ''; i.disabled = true; });
        return true;
      }).then(ok => { const nb = ok && $(`[data-approve="${st.id}"]`, box); if (nb) nb.click(); });
    }, true);
  }

  /* ------------------------------------------------------------ clone / delete */
  function cloneDialog(id, name) {
    N.modal(`<h2>کپی از قالب</h2><form class="form" id="tplCloneF"><label>نام قالب جدید<input name="cname" value="${esc(name + ' (کپی)')}" maxlength="80" required></label>
      <div class="row"><button class="btn primary" type="submit">ساخت کپی</button><button class="btn" type="button" id="tplCloneX">انصراف</button></div></form>`);
    const f = $('#tplCloneF'); $('#tplCloneX').onclick = closeModal; f.cname.select();
    f.onsubmit = e => { e.preventDefault(); busy(f.querySelector('[type=submit]'), 'در حال کپی…', async () => {
      const t = await api(`/api/templates/${encodeURIComponent(id)}/clone`, {method: 'POST', body: {name: f.cname.value.trim()}});
      closeModal(); toast('کپی ساخته شد: ' + t.name); N.go('templates', t.id); }); };
  }
  function deleteDialog(t) {
    N.modal(`<h2>حذف قالب</h2><p>قالب <b>${esc(t.name)}</b> و همه‌ی ${fa(t.version)} نسخه‌ی آن برای همیشه حذف می‌شود. برنامه‌هایی که از آن ساخته شده‌اند تغییری نمی‌کنند.</p>
      <form class="form" id="tplDelF"><label>برای تأیید، نام قالب را دقیقاً تایپ کنید<input name="confirm" autocomplete="off" placeholder="${esc(t.name)}"></label>
      <div class="row"><button class="btn danger" type="submit" disabled>حذف دائمی</button><button class="btn" type="button" id="tplDelX">انصراف</button></div></form>`);
    const f = $('#tplDelF'), go = f.querySelector('[type=submit]'); $('#tplDelX').onclick = closeModal; f.confirm.focus();
    f.confirm.oninput = () => { go.disabled = f.confirm.value.trim() !== t.name; };
    f.onsubmit = e => { e.preventDefault(); if (go.disabled) return; busy(go, 'در حال حذف…', async () => {
      await api(`/api/templates/${encodeURIComponent(t.id)}`, {method: 'DELETE', body: {confirm_name: f.confirm.value.trim()}});
      closeModal(); toast('قالب حذف شد'); N.go('templates'); }); };
  }

  /* ------------------------------------------------------------ editor (form + raw JSON) */
  function editor(data, t) {
    const v = $('#view'), creating = !t;
    const a = data.autoscale || {}, hc = data.health_check || {}, d = data.disk || {};
    const planOpts = sel => T.plans.map(p => `<option value="${esc(p.id)}"${p.id === sel ? ' selected' : ''}>${esc(p.name_fa)} (${esc(p.id)})</option>`).join('');
    v.innerHTML = `<div class="row" style="margin-bottom:10px"><button class="btn sm" id="tplEdBack">→ بازگشت</button>
      <span class="chip ${creating ? 'ok' : 'info'}">${creating ? 'قالب جدید' : `ویرایش «${esc(t.name)}» — نسخه‌ی فعلی ${fa(t.version)}؛ ذخیره نسخه‌ی ${fa(t.version + 1)} می‌سازد`}</span></div>
    <div class="grid g2">
      <div class="card"><h3>فرم</h3><form class="form" id="tplForm" autocomplete="off">
        <label>نام قالب<input name="tname" value="${esc(data.name)}" maxlength="80" required></label>
        <div class="grid g2"><label>stack<input name="stack" class="ltr" value="${esc(data.stack)}"></label><label>پورت<input name="port" class="ltr" inputmode="numeric" value="${esc(data.port)}"></label></div>
        <label>توضیح<textarea name="description_fa" rows="3">${esc(data.description_fa || '')}</textarea></label>
        <div class="grid g2"><label>پلن پیشنهادی<select name="plan">${planOpts(data.plan)}</select></label>
          <label>باندل<select name="bundle">${Object.entries(BUNDLE_FA).map(([k, l]) => `<option value="${k}"${k === data.bundle ? ' selected' : ''}>${esc(l)}</option>`).join('')}</select></label>
          <label>کمینه‌ی پلن خودران<select name="min_plan">${planOpts(a.min_plan)}</select></label><label>بیشینه‌ی پلن خودران<select name="max_plan">${planOpts(a.max_plan)}</select></label></div>
        <label>سقف هزینه‌ی روزانه (تومان)<input name="max_toman_day" class="ltr" inputmode="numeric" value="${esc(a.max_toman_day)}"></label>
        <label>مسیر سلامت<input name="health_path" class="ltr" value="${esc(data.health_path)}"></label>
        <label>دستور healthCheck (خالی = بدون بررسی)<input name="hc_command" class="ltr" value="${esc(hc.command || '')}"></label>
        <div class="grid g2"><label>interval (ثانیه)<input name="hc_interval" class="ltr" value="${esc(hc.interval ?? 30)}"></label><label>timeout<input name="hc_timeout" class="ltr" value="${esc(hc.timeout ?? 5)}"></label>
          <label>retries<input name="hc_retries" class="ltr" value="${esc(hc.retries ?? 3)}"></label><label>startPeriod<input name="hc_startPeriod" class="ltr" value="${esc(hc.startPeriod ?? 20)}"></label></div>
        <div class="grid g2"><label>نام دیسک (خالی = بدون دیسک)<input name="disk_name" class="ltr" value="${esc(d.name || '')}"></label><label>مسیر اتصال<input name="disk_mount" class="ltr" value="${esc(d.mountTo || '/data')}"></label>
          <label>حجم دیسک (گیگ)<input name="disk_size" class="ltr" value="${esc(d.size_gb || 1)}"></label></div>
        ${creating ? '' : '<label>یادداشت این نسخه<input name="note" placeholder="مثلاً: افزایش پورت"></label>'}
        <div id="tplFormErr"></div>
        <div class="row"><button class="btn primary" type="submit">${creating ? 'ساخت قالب' : 'ذخیره (نسخه‌ی جدید)'}</button><span class="muted small">متغیرها، پارامترها، cron و فایل‌ها را در ویرایشگر JSON تغییر دهید.</span></div>
      </form></div>
      <div class="card"><h3>ویرایشگر JSON</h3>
        <textarea id="tplJson" class="tpl-json" spellcheck="false">${esc(JSON.stringify(data, null, 2))}</textarea>
        <div id="tplJsonErr"></div>
        <div class="row" style="margin-top:8px"><button class="btn" id="tplCheck">✔ بررسی</button><button class="btn primary" id="tplJsonSave">${creating ? 'ساخت از JSON' : 'ذخیره‌ی JSON (نسخه‌ی جدید)'}</button></div>
      </div>
    </div>`;
    const f = $('#tplForm');
    $('#tplEdBack').onclick = () => creating ? N.go('templates') : viewOne(t.id);
    const num = x => (String(x).trim() === '' || isNaN(+x)) ? x : +x;
    const fields = () => ({name: f.tname.value.trim(), stack: f.stack.value.trim(), description_fa: f.description_fa.value, port: num(f.port.value),
      plan: f.plan.value, bundle: f.bundle.value, health_path: f.health_path.value.trim(),
      autoscale: {min_plan: f.min_plan.value, max_plan: f.max_plan.value, max_toman_day: num(f.max_toman_day.value)},
      health_check: f.hc_command.value.trim() ? {command: f.hc_command.value.trim(), interval: num(f.hc_interval.value), timeout: num(f.hc_timeout.value),
        retries: num(f.hc_retries.value), startPeriod: num(f.hc_startPeriod.value)} : null,
      disk: f.disk_name.value.trim() ? {name: f.disk_name.value.trim(), mountTo: f.disk_mount.value.trim(), size_gb: num(f.disk_size.value)} : null});
    const parse = () => { try { const o = JSON.parse($('#tplJson').value); if (!o || typeof o !== 'object' || Array.isArray(o)) throw new Error('ریشه باید یک شیء {…} باشد'); return o; }
      catch (e) { $('#tplJsonErr').innerHTML = errList('JSON نامعتبر: ' + e.message); return null; } };
    const save = async (body, errBox) => {
      $(errBox).innerHTML = '';
      try {
        const r = creating ? await api('/api/templates', {method: 'POST', body}) : await api(`/api/templates/${encodeURIComponent(t.id)}`, {method: 'PUT', body});
        toast(creating ? 'قالب ساخته شد' : `ذخیره شد — نسخه‌ی ${r.version}`); N.go('templates', r.id);
      } catch (e) { $(errBox).innerHTML = errList(e.message); toast('ذخیره نشد؛ خطاها را ببینید'); }
    };
    f.onsubmit = e => { e.preventDefault(); busy(f.querySelector('[type=submit]'), 'در حال ذخیره…', async () => {
      const base = creating ? (parse() || data) : {}; const body = {...base, ...fields()};
      if (!creating && f.note.value.trim()) body.note = f.note.value.trim();
      await save(body, '#tplFormErr'); }); };
    $('#tplCheck').onclick = e => { const o = parse(); if (!o) return; busy(e.currentTarget, 'در حال بررسی…', async () => {
      const r = await api('/api/templates/validate', {method: 'POST', body: o});
      $('#tplJsonErr').innerHTML = r.ok ? '<div class="chip ok" style="margin-top:6px">✔ معتبر است</div>' : errList(r.errors.join('\n')); }); };
    $('#tplJsonSave').onclick = e => { $('#tplJsonErr').innerHTML = ''; const o = parse(); if (!o) return;
      busy(e.currentTarget, 'در حال ذخیره…', () => save(o, '#tplJsonErr')); };
  }

  N.VIEWS.templates = async arg => (arg ? viewOne(arg) : list());
})();
