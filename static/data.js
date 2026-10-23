'use strict';
/* «پایگاه‌داده» and «ذخیره‌سازی»: Liara managed databases and object storage. Every change is an approval card; destructive
   cards (delete / restore) are approved only by typing the exact name; secrets (passwords, secret keys, signed links) are
   revealed once via POST. CSP-safe: no inline handlers; every server string goes through esc(). */
(() => {
  const N = window.Ngh; if (!N) return;
  const {api, esc, fa, toast, modal, propCard, bindProps, dt} = N;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  N.TITLES.databases = 'پایگاه‌داده‌ها'; N.TITLES.storage = 'ذخیره‌سازی ابری';
  const t$ = n => n == null ? '—' : fa(Math.round(n)) + ' تومان';
  const closeModal = () => $('#modal').classList.add('hidden');
  const TYPED = {delete_database: 'database', restore_database_backup: 'database', delete_bucket: 'bucket'};
  const ST = {RUNNING: ['ok', 'در حال اجرا'], STOPPED: ['bad', 'خاموش'], CREATING: ['info', 'در حال ساخت'], ACTIVE: ['ok', 'فعال']};
  const ENGINES = ['postgres', 'mysql', 'mariadb', 'mongodb', 'redis', 'mssql'];

  async function busy(btn, text, fn) {
    if (!btn || btn.disabled) return null;
    const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="spin"></span> ${esc(text)}`;
    try { return await fn(); } catch (e) { toast(e.message, 6500); return null; } finally { if (btn.isConnected) { btn.disabled = false; btn.innerHTML = old; } }
  }
  async function propose(tool, args) {
    const r = await api('/api/diag/run', {method: 'POST', body: {tool, args}});
    if (r.error) { toast(r.error, 7000); return; }
    const p = r.proposal || {}; if (p.refused || p.error) { toast(p.error || 'رد شد', 7000); return; }
    const all = await api('/api/proposals').catch(() => []); const full = (all || []).find(x => x.id === p.id) || p;
    if (TYPED[tool]) return typedModal(full, args[TYPED[tool]]);
    const im = full.impact || {};
    const names = im.env_names ? `<div class="small"><b>متغیرهایی که نوشته می‌شوند (فقط نام):</b> ${im.env_names.map(n => `<span class="chip ltr">${esc(n)}</span>`).join(' ')}
      ${(im.overwrites || []).length ? `<div class="muted">جایگزین می‌شوند: ${esc(im.overwrites.join(', '))}</div>` : ''}</div>` : '';
    modal(`<h3>کارت تأیید</h3>${propCard(full)}${names}<div class="muted small">فقط پس از تأیید شما اجرا می‌شود.</div>`); bindProps();
  }
  function typedModal(p, name) {
    const im = p.impact || {};
    modal(`<h3>${esc(p.title || '')}</h3><div class="facts"><div class="fact"><small>اثر</small>${esc(im.downtime || '')}</div><div class="fact"><small>ریسک</small>${esc(im.risk || '')}</div></div>
      <label class="small">برای تأیید، نام «${esc(name)}» را دقیقاً تایپ کنید<input id="tyName" class="ltr" autocomplete="off"></label>
      <div class="row" style="margin-top:10px"><button class="btn danger" id="tyGo">تأیید و اجرا</button><button class="btn" id="tyNo">انصراف</button></div>`);
    $('#tyNo').addEventListener('click', async () => { await api(`/api/proposals/${p.id}/reject`, {method: 'POST'}).catch(() => 0); closeModal(); });
    $('#tyGo').addEventListener('click', e => busy(e.currentTarget, 'در حال اجرا', async () => {
      await api(`/api/diag/approve-typed/${p.id}`, {method: 'POST', body: {confirm_name: $('#tyName').value}}); closeModal(); toast('تأیید شد؛ در حال اجرا…');
    }));
  }
  async function reveal(path) {
    const r = await api(path, {method: 'POST'});
    modal(`<h3>${esc(r.label)}</h3><div class="muted small">فقط همین یک بار نمایش داده می‌شود؛ در جای امن ذخیره کنید.</div><pre class="logbox" style="white-space:pre-wrap;user-select:all">${esc(r.value)}</pre><button class="btn" id="cpS">کپی</button>`);
    $('#cpS').addEventListener('click', () => navigator.clipboard && navigator.clipboard.writeText(r.value).then(() => toast('کپی شد')));
  }
  async function secretsCard(tools) {
    const ps = await api('/api/proposals').catch(() => []);
    const rows = (ps || []).filter(p => tools.includes(p.tool) && p.result && p.result.secret_once);
    return rows.length ? `<div class="card" style="margin-bottom:14px"><h3>🔑 موارد محرمانه‌ی یک‌بار مصرف</h3>${rows.map(p => `<div class="row"><span>${esc(p.title)}</span><span class="muted small">${dt(p.decided_at || p.created_at)}</span>
      <button class="btn sm primary" data-reveal="${esc(p.result.secret_once)}">نمایش یک‌باره</button></div>`).join('')}</div>` : '';
  }
  const bindReveal = () => $$('[data-reveal]').forEach(b => b.addEventListener('click', () => busy(b, '…', () => reveal(b.dataset.reveal))));
  async function appNames() { try { return (await api('/api/diag/apps')).filter(a => !a.locked).map(a => a.name); } catch { return []; } }

  /* ------------------------------------------------------------ databases */
  async function newDb() {
    const [cat, apps] = await Promise.all([api('/api/diag/databases/catalog'), appNames()]);
    const planOpts = eng => cat.plans.filter(p => (p.engines || []).includes(eng)).map(p => `<option value="${esc(p.plan)}">${esc(p.plan)} · ${fa(p.cpu)} هسته · ${fa(p.ram_gb)} گیگ · ${fa(p.disk_gb)} گیگ — ${t$((p.prices.basic || {}).toman_per_hour)}/ساعت</option>`).join('');
    modal(`<h3>پایگاه‌داده‌ی جدید</h3><label class="small">نام (لاتین کوچک)<input id="ndName" class="ltr" placeholder="shop-db"></label>
      <label class="small">موتور<select id="ndEng">${ENGINES.filter(e => cat.versions[e]).map(e => `<option>${e}</option>`).join('')}</select></label>
      <label class="small">نسخه<select id="ndVer" class="ltr"></select></label><label class="small">پلن<select id="ndPlan"></select></label>
      <label class="small">باندل<select id="ndBundle"><option value="basic">basic (بدون پشتیبان)</option><option value="standard">standard (پشتیبان روزانه)</option><option value="pro">pro</option></select></label>
      <label class="small">اتصال به برنامه (اختیاری)<select id="ndApp"><option value="">— بدون اتصال —</option>${apps.map(a => `<option>${esc(a)}</option>`).join('')}</select></label>
      <div class="muted small">پیش از ساخت، موجودی برای یک هفته هزینه بررسی می‌شود. هزینه ساعتی است.</div><div class="row" style="margin-top:10px"><button class="btn primary" id="ndGo">ساخت کارت تأیید</button></div>`);
    const sync = () => { const e = $('#ndEng').value; $('#ndVer').innerHTML = (cat.versions[e] || []).map(v => `<option>${esc(v)}</option>`).join(''); $('#ndPlan').innerHTML = planOpts(e); };
    sync(); $('#ndEng').addEventListener('change', sync);
    $('#ndGo').addEventListener('click', e => busy(e.currentTarget, '…', () => {
      const a = {name: $('#ndName').value.trim(), engine: $('#ndEng').value, version: $('#ndVer').value, plan: $('#ndPlan').value, bundle: $('#ndBundle').value};
      if ($('#ndApp').value) a.connect_app = $('#ndApp').value; return propose('create_database', a);
    }));
  }
  async function dbDetails(name) {
    const [d, apps] = await Promise.all([api('/api/diag/databases/' + encodeURIComponent(name)), appNames()]);
    if (d.error) { toast(d.error); return; }
    modal(`<h3 class="ltr">${esc(d.name)}</h3><div class="facts">${[['موتور', `${d.engine} ${d.version || ''}`], ['وضعیت', d.status], ['پلن', `${d.plan} · ${d.bundle || ''}`], ['میزبان داخلی', `${d.internal_host}:${d.internal_port}`],
        ['میزبان عمومی', d.public_network ? `${d.public_host}:${d.public_port}` : 'غیرفعال'], ['نام دیتابیس', d.db_name], ['کاربر', d.username], ['دیسک', d.disk_gb ? d.disk_gb + ' GB' : '—'], ['هزینه', `${t$(d.toman_per_hour)}/ساعت`], ['رمز', 'پنهان']]
        .map(([k, v]) => `<div class="fact"><small>${esc(k)}</small><span class="ltr">${esc(v ?? '—')}</span></div>`).join('')}</div>
      <h4>اتصال به برنامه</h4><div class="row"><select id="cdApp">${apps.map(a => `<option>${esc(a)}</option>`).join('')}</select><button class="btn sm primary" id="cdGo">ساخت کارت اتصال</button></div>
      <h4>پشتیبان‌ها</h4>${(d.backups || []).map(b => `<div class="row small"><span class="ltr">${esc(b.name)}</span><span class="muted">${esc(b.at || '')} · ${esc(b.mb)} MB</span>
        <button class="btn sm" data-bl="${esc(b.name)}">لینک دانلود</button><button class="btn sm ghost" data-br="${esc(b.name)}">بازیابی</button></div>`).join('') || `<div class="muted small">${esc(d.backups_error ? 'خطا در خواندن پشتیبان‌ها' : 'پشتیبانی نیست')}</div>`}
      <div class="row" style="margin-top:6px"><button class="btn sm" id="bkNow">پشتیبان‌گیری الان</button><button class="btn sm" id="pwReset">بازنشانی رمز</button>
        <button class="btn sm ghost" id="pubT">${d.public_network ? 'غیرفعال کردن دسترسی عمومی' : 'فعال کردن دسترسی عمومی'}</button></div>`);
    const D = d.name;
    $('#cdGo').addEventListener('click', e => busy(e.currentTarget, '…', () => propose('connect_database', {database: D, app: $('#cdApp').value})));
    $('#bkNow').addEventListener('click', e => busy(e.currentTarget, '…', () => propose('backup_database', {database: D})));
    $('#pwReset').addEventListener('click', e => busy(e.currentTarget, '…', () => propose('reset_database_password', {database: D})));
    $('#pubT').addEventListener('click', e => busy(e.currentTarget, '…', () => propose('database_public_network', {database: D, enable: !d.public_network})));
    $$('[data-bl]').forEach(b => b.addEventListener('click', () => busy(b, '…', () => propose('database_backup_link', {database: D, backup: b.dataset.bl}))));
    $$('[data-br]').forEach(b => b.addEventListener('click', () => busy(b, '…', () => propose('restore_database_backup', {database: D, backup: b.dataset.br}))));
  }
  async function resizeDb(db) {
    const cat = await api('/api/diag/databases/catalog?engine=' + encodeURIComponent(db.engine));
    modal(`<h3>تغییر پلن ${esc(db.name)}</h3><select id="rdPlan">${cat.plans.map(p => `<option value="${esc(p.plan)}" ${p.plan === db.plan ? 'selected' : ''}>${esc(p.plan)} · ${fa(p.cpu)} هسته · ${fa(p.ram_gb)} گیگ — ${t$((p.prices[db.bundle || 'basic'] || p.prices.basic || {}).toman_per_hour)}/ساعت</option>`).join('')}</select>
      <div class="row" style="margin-top:10px"><button class="btn primary" id="rdGo">ساخت کارت تأیید</button></div>`);
    $('#rdGo').addEventListener('click', e => busy(e.currentTarget, '…', () => propose('resize_database', {database: db.name, plan: $('#rdPlan').value})));
  }
  N.VIEWS.databases = async () => {
    const v = $('#view'); v.innerHTML = '<div class="card"><span class="spin"></span> در حال خواندن پایگاه‌داده‌ها…</div>';
    const [L, sec] = await Promise.all([api('/api/diag/databases'), secretsCard(['reset_database_password', 'database_backup_link'])]);
    if (L.error) { v.innerHTML = `<div class="card err">${esc(L.error)}</div>`; return; }
    v.innerHTML = `${sec}<div class="card" style="margin-bottom:14px"><div class="row"><b>${fa(L.count)} پایگاه‌داده</b><span class="chip info">جمع هزینه: ${t$(L.total_toman_per_hour)} در ساعت</span>
        <span class="muted small">رمزها هرگز نمایش داده نمی‌شوند؛ اتصال به برنامه متغیرها را مستقیم در لیارا می‌نویسد.</span><button class="btn primary right" id="newDb">+ پایگاه‌داده‌ی جدید</button></div></div>
      <div class="grid g2">${(L.databases || []).map(d => { const [c, t] = ST[d.status] || ['info', d.status || '—']; return `<div class="card" data-db="${esc(d.name)}">
        <div class="row"><b class="ltr">${esc(d.name)}</b><span class="chip ${c}">${esc(d.status_fa || t)}</span><span class="chip ltr">${esc(d.engine)} ${esc(d.version || '')}</span><span class="right muted small">${t$(d.toman_per_hour)}/ساعت</span></div>
        <div class="small muted ltr">${esc(d.plan)} · ${esc(d.bundle || '')}${d.public_network ? ' · public' : ''}</div>
        <div class="row" style="margin-top:8px"><button class="btn sm" data-dact="details">جزئیات و اتصال</button>${d.status === 'RUNNING' ? '<button class="btn sm" data-dact="restart">ری‌استارت</button><button class="btn sm" data-dact="stop">خاموش</button>' : '<button class="btn sm" data-dact="start">روشن</button>'}
          <button class="btn sm" data-dact="resize">تغییر پلن</button><button class="btn sm danger" data-dact="delete">حذف</button></div></div>`; }).join('') || '<div class="card muted">هنوز پایگاه‌داده‌ای ندارید.</div>'}</div>`;
    $('#newDb').addEventListener('click', () => newDb()); bindReveal();
    $$('[data-db]').forEach(card => { const d = L.databases.find(x => x.name === card.dataset.db);
      $$('[data-dact]', card).forEach(b => b.addEventListener('click', () => { const a = b.dataset.dact;
        if (a === 'details') return busy(b, '…', () => dbDetails(d.name));
        if (['start', 'stop', 'restart'].includes(a)) return busy(b, '…', () => propose('database_power', {database: d.name, action: a}));
        if (a === 'resize') return resizeDb(d);
        if (a === 'delete') return busy(b, '…', () => propose('delete_database', {database: d.name}));
      })); });
  };

  /* ------------------------------------------------------------ object storage */
  async function newBucket() {
    const [cat, apps] = await Promise.all([api('/api/diag/storage/catalog'), appNames()]);
    modal(`<h3>باکت جدید</h3><label class="small">نام (لاتین کوچک، یکتا در لیارا)<input id="nbName" class="ltr" placeholder="shop-files"></label>
      <label class="small">پلن<select id="nbPlan">${cat.plans.map(p => `<option value="${esc(p.plan)}">${esc(p.plan)} · ${fa(p.space_gb)} گیگ — ${t$(p.toman_per_hour)}/ساعت (${t$(p.toman_per_month)}/ماه)</option>`).join('')}</select></label>
      <label class="small">دسترسی<select id="nbPerm"><option value="private">خصوصی</option><option value="public">عمومی (خواندن بدون احراز هویت)</option></select></label>
      <label class="small">اتصال به برنامه (اختیاری)<select id="nbApp"><option value="">— بدون اتصال —</option>${apps.map(a => `<option>${esc(a)}</option>`).join('')}</select></label>
      <div class="row" style="margin-top:10px"><button class="btn primary" id="nbGo">ساخت کارت تأیید</button></div>`);
    $('#nbGo').addEventListener('click', e => busy(e.currentTarget, '…', () => {
      const a = {name: $('#nbName').value.trim(), plan: $('#nbPlan').value, permission: $('#nbPerm').value}; if ($('#nbApp').value) a.connect_app = $('#nbApp').value; return propose('create_bucket', a);
    }));
  }
  async function objects(name) {
    const r = await api(`/api/diag/storage/${encodeURIComponent(name)}/objects`);
    const apps = await appNames();
    modal(`<h3 class="ltr">${esc(name)}</h3><div class="row small"><span class="chip">${fa(r.count)} فایل</span>${(r.folders || []).map(f => `<span class="chip ltr">📁 ${esc(f)}</span>`).join('')}</div>
      <div class="tablewrap"><table class="dg-tbl">${(r.objects || []).map(o => `<tr><td class="ltr">${esc(o.key)}</td><td class="ltr">${esc(o.kb)} KB</td><td><button class="btn sm" data-ol="${esc(o.key)}">لینک موقت</button></td></tr>`).join('') || '<tr><td class="muted">خالی</td></tr>'}</table></div>
      <h4>اتصال به برنامه</h4><div class="row"><select id="cbApp">${apps.map(a => `<option>${esc(a)}</option>`).join('')}</select><select id="cbStyle"><option value="liara">LIARA_*</option><option value="aws">AWS_* / S3_BUCKET</option></select><button class="btn sm primary" id="cbGo">ساخت کارت اتصال</button></div>`);
    $$('[data-ol]').forEach(b => b.addEventListener('click', () => busy(b, '…', () => propose('bucket_file_link', {bucket: name, key: b.dataset.ol, expiry: 3600}))));
    $('#cbGo').addEventListener('click', e => busy(e.currentTarget, '…', () => propose('connect_bucket', {bucket: name, app: $('#cbApp').value, style: $('#cbStyle').value})));
  }
  N.VIEWS.storage = async () => {
    const v = $('#view'); v.innerHTML = '<div class="card"><span class="spin"></span> در حال خواندن باکت‌ها…</div>';
    const [L, K, sec, cat] = await Promise.all([api('/api/diag/storage'), api('/api/diag/storage/keys'), secretsCard(['create_storage_key', 'revoke_storage_key', 'bucket_file_link']), api('/api/diag/storage/catalog')]);
    if (L.error) { v.innerHTML = `<div class="card err">${esc(L.error)}</div>`; return; }
    v.innerHTML = `${sec}<div class="card" style="margin-bottom:14px"><div class="row"><b>${fa(L.count)} باکت</b><span class="chip info">جمع هزینه: ${t$(L.total_toman_per_hour)} در ساعت</span>
        <span class="chip ltr">${esc(L.endpoint)}</span><button class="btn primary right" id="newB">+ باکت جدید</button></div></div>
      <div class="grid g2">${(L.buckets || []).map(b => `<div class="card" data-bk="${esc(b.name)}"><div class="row"><b class="ltr">${esc(b.name)}</b><span class="chip ${b.permission === 'public' ? 'warn' : 'ok'}">${b.permission === 'public' ? 'عمومی' : 'خصوصی'}</span>
          <span class="right muted small">${t$(b.toman_per_hour)}/ساعت</span></div><div class="small">${esc(b.plan)} · ${b.used_mb != null ? fa(b.used_mb) + ' MB از ' : ''}${fa(b.space_gb)} گیگ · ${b.objects != null ? fa(b.objects) + ' فایل' : ''}</div>
        <div class="row" style="margin-top:8px"><button class="btn sm" data-bact="objects">فایل‌ها و اتصال</button><button class="btn sm" data-bact="perm">${b.permission === 'public' ? 'خصوصی کن' : 'عمومی کن'}</button>
          <select class="sm" data-bplan style="max-width:150px">${cat.plans.map(p => `<option value="${esc(p.plan)}" ${p.plan === b.plan ? 'selected' : ''}>${esc(p.plan)}</option>`).join('')}</select><button class="btn sm" data-bact="resize">ارتقا</button>
          <button class="btn sm danger" data-bact="delete">حذف</button></div></div>`).join('') || '<div class="card muted">هنوز باکتی ندارید.</div>'}</div>
      <div class="card" style="margin-top:14px"><h3>کلیدهای دسترسی</h3>${(K.keys || []).map(k => `<div class="row small"><span class="ltr">${esc(k.access_key)}</span><span>${esc(k.description || '')}</span><span class="muted ltr">${esc((k.buckets || []).join(', '))} · ${esc((k.permissions || []).join(','))}</span>
          <button class="btn sm" data-kr="${esc(k.id)}">چرخش رمز</button><button class="btn sm ghost" data-kd="${esc(k.id)}">حذف</button></div>`).join('') || '<div class="muted small">کلیدی نیست</div>'}
        <div class="row" style="margin-top:8px"><select id="nkB">${(L.buckets || []).map(b => `<option>${esc(b.name)}</option>`).join('')}</select><input id="nkD" placeholder="توضیح" style="max-width:200px"><button class="btn sm primary" id="nkGo">کلید جدید</button></div></div>`;
    $('#newB').addEventListener('click', () => newBucket()); bindReveal();
    $$('[data-bk]').forEach(card => { const b = L.buckets.find(x => x.name === card.dataset.bk);
      $$('[data-bact]', card).forEach(x => x.addEventListener('click', () => { const a = x.dataset.bact;
        if (a === 'objects') return busy(x, '…', () => objects(b.name));
        if (a === 'perm') return busy(x, '…', () => propose('bucket_permission', {bucket: b.name, permission: b.permission === 'public' ? 'private' : 'public'}));
        if (a === 'resize') return busy(x, '…', () => propose('resize_bucket', {bucket: b.name, plan: $('[data-bplan]', card).value}));
        if (a === 'delete') return busy(x, '…', () => propose('delete_bucket', {bucket: b.name}));
      })); });
    $$('[data-kr]').forEach(b => b.addEventListener('click', () => busy(b, '…', () => propose('revoke_storage_key', {key: b.dataset.kr}))));
    $$('[data-kd]').forEach(b => b.addEventListener('click', () => busy(b, '…', () => propose('delete_storage_key', {key: b.dataset.kd}))));
    if ($('#nkGo')) $('#nkGo').addEventListener('click', e => busy(e.currentTarget, '…', () => propose('create_storage_key', {buckets: [$('#nkB').value], description: $('#nkD').value || 'negahban'})));
  };
})();
