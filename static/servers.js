'use strict';
/* «سرورها»: Liara virtual servers (IaaS) and server templates. Every change becomes an approval card (create, power, resize,
   disk, password, network, delete with type-the-name). Reset passwords are revealed once via POST. Also appends the server
   templates section to the templates page. CSP-safe: no inline handlers; every server string goes through esc(). */
(() => {
  const N = window.Ngh; if (!N) return;
  const {api, esc, fa, toast, modal, propCard, bindProps, dt} = N;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  N.TITLES.servers = 'سرورهای مجازی';
  const POWER = {start: 'روشن کردن', stop: 'خاموش کردن', reboot: 'راه‌اندازی مجدد', shutdown: 'قطع برق'};
  const PW = {POWERED_ON: ['ok', 'روشن'], POWERED_OFF: ['bad', 'خاموش'], SUSPENDED: ['warn', 'معلق']};
  const C = {catalog: null};
  const t$ = n => n == null ? '—' : fa(Math.round(n)) + ' تومان';
  const closeModal = () => $('#modal').classList.add('hidden');

  async function busy(btn, text, fn) {
    if (!btn || btn.disabled) return null;
    const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="spin"></span> ${esc(text)}`;
    try { return await fn(); } catch (e) { toast(e.message, 6500); return null; } finally { if (btn.isConnected) { btn.disabled = false; btn.innerHTML = old; } }
  }
  async function catalog() { if (!C.catalog) C.catalog = await api('/api/diag/servers/catalog'); return C.catalog; }
  const planLabel = p => `${p.plan} · ${fa(p.cpu)} هسته · ${fa(p.ram_gb)} گیگ · ${fa(p.disk_gb)} گیگ دیسک · ${t$(p.toman_per_hour)}/ساعت`;

  /* proposals: show the card; delete_server gets its own type-the-name approval */
  async function propose(tool, args) {
    const r = await api('/api/diag/run', {method: 'POST', body: {tool, args}});
    if (r.error) { toast(r.error, 6500); return; }
    const p = r.proposal || {}; if (p.refused || p.error) { toast(p.error || 'رد شد', 6500); return; }
    const all = await api('/api/proposals').catch(() => []); const full = (all || []).find(x => x.id === p.id);
    if (tool === 'delete_server') { deleteModal(full || p, args.server); return; }
    modal(`<h3>کارت تأیید</h3>${full ? propCard(full) : `<div>${esc(p.title || '')}</div>`}<div class="muted small">این اقدام فقط پس از تأیید شما اجرا می‌شود.</div>`);
    bindProps();
  }
  function deleteModal(p, name) {
    const im = p.impact || {};
    modal(`<h3>${esc(p.title || 'حذف سرور')}</h3><div class="facts"><div class="fact"><small>اثر</small>${esc(im.downtime || '')}</div><div class="fact"><small>هزینه</small>${esc(im.cost || '')}</div><div class="fact"><small>ریسک</small>${esc(im.risk || '')}</div></div>
      <label class="small">برای تأیید، نام سرور «${esc(name)}» را دقیقاً تایپ کنید<input id="dsName" class="ltr" autocomplete="off"></label>
      <div class="row" style="margin-top:10px"><button class="btn danger" id="dsGo">حذف دائمی</button><button class="btn" id="dsNo">انصراف</button></div>`);
    $('#dsNo').addEventListener('click', async () => { await api(`/api/proposals/${p.id}/reject`, {method: 'POST'}).catch(() => 0); closeModal(); });
    $('#dsGo').addEventListener('click', e => busy(e.currentTarget, 'در حال حذف', async () => {
      await api(`/api/diag/servers/approve-delete/${p.id}`, {method: 'POST', body: {confirm_name: $('#dsName').value}});
      closeModal(); toast('تأیید شد؛ سرور در حال حذف است'); setTimeout(() => N.S.view === 'servers' && N.VIEWS.servers(), 4000);
    }));
  }
  async function revealSecret(path) {
    const r = await api(path, {method: 'POST'});
    modal(`<h3>${esc(r.label)}</h3><div class="muted small">فقط همین یک بار نمایش داده می‌شود؛ آن را در جای امن ذخیره کنید.</div>
      <pre class="logbox" style="font-size:16px;user-select:all">${esc(r.value)}</pre><button class="btn" id="cpPw">کپی</button>`);
    $('#cpPw').addEventListener('click', () => navigator.clipboard && navigator.clipboard.writeText(r.value).then(() => toast('کپی شد')));
  }

  /* ------------------------------------------------------------ servers page */
  function serverCard(s) {
    const [cls, txt] = PW[s.power] || ['info', s.state_fa || s.state || '—'];
    return `<div class="card" data-srv="${esc(s.name)}"><div class="row"><b class="ltr">${esc(s.name)}</b><span class="chip ${cls}">${esc(txt)}</span>
        ${s.state && s.state !== 'CREATED' ? `<span class="chip info">${esc(s.state)}</span>` : ''}${s.protected ? '<span class="chip warn">🔒 محافظت‌شده</span>' : ''}<span class="right muted small">${t$(s.toman_per_hour)} در ساعت</span></div>
      <div class="facts"><div class="fact"><small>پلن</small><span class="ltr">${esc(s.plan)}</span></div><div class="fact"><small>منابع</small>${fa(s.cpu)} هسته · ${fa(s.ram_gb)} گیگ · ${fa(s.disk_gb)} گیگ</div>
        <div class="fact"><small>سیستم‌عامل</small><span class="ltr">${esc(s.os)}</span></div><div class="fact"><small>IP</small><span class="ltr">${esc((s.ips || []).join(', ') || '—')}</span></div>
        ${s.deletable_from ? `<div class="fact"><small>قابل حذف از</small><span class="ltr">${esc(s.deletable_from)}</span></div>` : ''}</div>
      <div class="row" style="margin-top:8px"><button class="btn sm" data-sact="details">جزئیات</button>
        ${s.power === 'POWERED_ON' ? '<button class="btn sm" data-sact="reboot">راه‌اندازی مجدد</button><button class="btn sm" data-sact="stop">خاموش</button>' : '<button class="btn sm" data-sact="start">روشن</button>'}
        <button class="btn sm" data-sact="resize">تغییر پلن</button><button class="btn sm" data-sact="password">رمز root</button>
        <button class="btn sm ghost" data-sact="protect">${s.protected ? 'برداشتن محافظت' : 'محافظت'}</button><button class="btn sm danger" data-sact="delete">حذف</button></div></div>`;
  }
  async function details(name) {
    const d = await api('/api/diag/servers/' + encodeURIComponent(name));
    if (d.error) { toast(d.error); return; }
    modal(`<h3 class="ltr">${esc(d.name)}</h3><div class="facts">${[['وضعیت', `${d.state || ''} / ${d.power || ''} / ${d.guest || ''}`], ['پلن', d.plan], ['منابع', `${d.cpu} CPU · ${d.ram_gb} GB · ${d.disk_gb} GB`],
        ['سیستم‌عامل', d.os], ['IP', (d.ips || []).join(', ')], ['پورت SSH', d.ssh_port], ['hostname', d.hostname], ['ترافیک ۲۴ ساعت', d.traffic_24h_mb != null ? d.traffic_24h_mb + ' MB' : '—'],
        ['هزینه', `${t$(d.toman_per_hour)}/ساعت · ${t$(d.toman_per_month)}/ماه`], ['دیسک آزاد', d.disk_space_available_gb != null ? d.disk_space_available_gb + ' GB' : '—']]
        .map(([k, v]) => `<div class="fact"><small>${esc(k)}</small><span class="ltr">${esc(v ?? '—')}</span></div>`).join('')}</div>
      <h4>دیسک‌ها</h4>${(d.disks || []).map(x => `<span class="chip ltr">${esc(x.name || x._id)} · ${esc(x.size)} GB</span>`).join(' ') || '<span class="muted small">—</span>'}
      <div class="row" style="margin-top:6px"><input id="dkSize" type="number" min="1" max="640" placeholder="GB" style="max-width:90px"><button class="btn sm" id="dkAdd">افزودن دیسک</button><button class="btn sm" id="netReset">بازنشانی شبکه</button></div>
      <h4>عملیات اخیر</h4><div class="tablewrap"><table class="dg-tbl">${(d.operations || []).map(o => `<tr><td class="ltr">${esc(o.name)}</td><td>${esc(o.state)}</td><td class="ltr small">${esc(o.createdAt || '')}</td></tr>`).join('') || '<tr><td class="muted">—</td></tr>'}</table></div>`);
    $('#dkAdd').addEventListener('click', e => { const n = +$('#dkSize').value; if (n > 0) busy(e.currentTarget, '…', () => propose('server_add_disk', {server: d.name, size_gb: n})); });
    $('#netReset').addEventListener('click', e => busy(e.currentTarget, '…', () => propose('server_reset_network', {server: d.name})));
  }
  async function resizeDlg(s) {
    const cat = await catalog();
    modal(`<h3>تغییر پلن ${esc(s.name)}</h3><select id="rzPlan">${cat.plans.map(p => `<option value="${esc(p.plan)}" ${p.plan === s.plan ? 'selected' : ''}>${esc(planLabel(p))}</option>`).join('')}</select>
      <div class="row" style="margin-top:10px"><button class="btn primary" id="rzGo">ساخت کارت تأیید</button></div>`);
    $('#rzGo').addEventListener('click', e => busy(e.currentTarget, '…', () => propose('resize_server', {server: s.name, plan: $('#rzPlan').value})));
  }
  async function createDlg(tpl) {
    const [cat, T] = await Promise.all([catalog(), api('/api/diag/server-templates')]);
    modal(`<h3>ساخت سرور مجازی</h3><label class="small">نام (حروف کوچک لاتین، عدد و -)<input id="nsName" class="ltr" placeholder="my-server"></label>
      <label class="small">قالب<select id="nsTpl"><option value="">— بدون قالب (انتخاب دستی) —</option>${T.templates.map(t => `<option value="${esc(t.id)}" ${t.id === tpl ? 'selected' : ''}>${esc(t.name)} — ${t$(t.toman_per_hour)}/ساعت</option>`).join('')}</select></label>
      <div id="nsManual"><label class="small">پلن<select id="nsPlan">${cat.plans.map(p => `<option value="${esc(p.plan)}">${esc(planLabel(p))}</option>`).join('')}</select></label>
        <label class="small">سیستم‌عامل<select id="nsOs" class="ltr">${cat.images.filter(i => !i.os.startsWith('windows')).map(i => `<option value="${esc(i.os)}">${esc(i.os)}</option>`).join('')}</select></label></div>
      <label class="small">کلید عمومی SSH (اختیاری)<input id="nsKey" class="ltr" placeholder="ssh-ed25519 AAAA…"></label>
      <div class="muted small">هزینه از لحظه‌ی ساخت تا حذف، ساعتی محاسبه می‌شود. پس از تأیید شما ساخته می‌شود.</div>
      <div class="row" style="margin-top:10px"><button class="btn primary" id="nsGo">ساخت کارت تأیید</button></div>`);
    const sync = () => { $('#nsManual').style.display = $('#nsTpl').value ? 'none' : ''; }; sync(); $('#nsTpl').addEventListener('change', sync);
    $('#nsGo').addEventListener('click', e => busy(e.currentTarget, '…', () => {
      const a = {name: $('#nsName').value.trim()}; if ($('#nsTpl').value) a.template = $('#nsTpl').value; else { a.plan = $('#nsPlan').value; a.os = $('#nsOs').value; }
      if ($('#nsKey').value.trim()) a.ssh_keys = [$('#nsKey').value.trim()];
      return propose('create_server', a);
    }));
  }
  async function secretsCard() {
    const ps = await api('/api/proposals').catch(() => []);
    const rows = (ps || []).filter(p => p.tool === 'server_reset_password' && p.result && p.result.secret_once);
    return rows.length ? `<div class="card" style="margin-bottom:14px"><h3>رمزهای تازه (یک‌بار مصرف)</h3>${rows.map(p => `<div class="row"><span class="ltr">${esc((p.args || {}).server)}</span><span class="muted small">${dt(p.decided_at || p.created_at)}</span>
      <button class="btn sm primary" data-secret="${esc(p.result.secret_once)}">نمایش یک‌باره‌ی رمز</button></div>`).join('')}</div>` : '';
  }

  N.VIEWS.servers = async () => {
    const v = $('#view'); v.innerHTML = '<div class="card"><span class="spin"></span> در حال خواندن سرورها…</div>';
    const [L, sec] = await Promise.all([api('/api/diag/servers'), secretsCard()]);
    if (L.error) { v.innerHTML = `<div class="card err">${esc(L.error)}</div>`; return; }
    v.innerHTML = `${sec}<div class="card" style="margin-bottom:14px"><div class="row"><b>${fa(L.count)} سرور</b><span class="chip info">جمع هزینه: ${t$(L.total_toman_per_hour)} در ساعت</span>
        <span class="muted small">${esc(L.note || '')}</span><button class="btn primary right" id="newSrv">+ سرور جدید</button></div></div>
      <div class="grid g2">${(L.servers || []).map(serverCard).join('') || '<div class="card muted">هنوز سرور مجازی ندارید. از یک قالب بسازید 👇</div>'}</div>
      <div class="card" style="margin-top:14px"><h3>قالب‌های سرور</h3><div id="srvTpls"><span class="spin"></span></div></div>`;
    $('#newSrv').addEventListener('click', () => createDlg());
    $$('[data-secret]').forEach(b => b.addEventListener('click', () => busy(b, '…', () => revealSecret(b.dataset.secret))));
    $$('[data-srv]').forEach(card => {
      const s = L.servers.find(x => x.name === card.dataset.srv);
      $$('[data-sact]', card).forEach(b => b.addEventListener('click', () => {
        const a = b.dataset.sact;
        if (a === 'details') return busy(b, '…', () => details(s.name));
        if (POWER[a]) return busy(b, '…', () => propose('server_power', {server: s.name, action: a}));
        if (a === 'resize') return resizeDlg(s);
        if (a === 'password') return busy(b, '…', () => propose('server_reset_password', {server: s.name}));
        if (a === 'delete') return busy(b, '…', () => propose('delete_server', {server: s.name}));
        if (a === 'protect') return busy(b, '…', async () => { await api('/api/diag/servers/protect', {method: 'POST', body: {server: s.name, on: !s.protected}}); N.VIEWS.servers(); });
      }));
    });
    renderTemplates($('#srvTpls'));
  };

  /* ------------------------------------------------------------ server templates (also on the templates page) */
  async function renderTemplates(box) {
    if (!box) return;
    const T = await api('/api/diag/server-templates');
    box.innerHTML = `<div class="grid g3">${T.templates.map(t => `<div class="card" data-stpl="${esc(t.id)}"><b>${esc(t.name)}</b>
        <div class="row small">${t.builtin ? '<span class="chip">پیش‌فرض</span>' : `<span class="chip info">نسخه ${fa(t.version)}</span>`}${t.has_init_script ? '<span class="chip">اسکریپت راه‌اندازی</span>' : ''}<span class="chip ltr">${esc(t.os)}</span></div>
        <div class="muted small">${esc(t.description_fa || '')}</div><div class="small">${fa(t.cpu)} هسته · ${fa(t.ram_gb)} گیگ · ${fa(t.disk_gb)} گیگ — <b>${t$(t.toman_per_hour)}</b>/ساعت (${t$(t.toman_per_month)}/ماه)</div>
        <div class="row"><button class="btn sm primary" data-tact="create">ساخت سرور</button><button class="btn sm" data-tact="view">مشاهده</button><button class="btn sm" data-tact="clone">کپی</button>
          ${t.builtin ? '' : '<button class="btn sm" data-tact="edit">ویرایش</button><button class="btn sm ghost" data-tact="delete">حذف</button>'}</div></div>`).join('')}</div>`;
    $$('[data-stpl]', box).forEach(card => $$('[data-tact]', card).forEach(b => b.addEventListener('click', () => {
      const id = card.dataset.stpl, a = b.dataset.tact;
      if (a === 'create') return createDlg(id);
      if (a === 'view' || a === 'edit') return busy(b, '…', () => editTpl(id, a === 'edit'));
      if (a === 'clone') return busy(b, '…', async () => { await api(`/api/diag/server-templates/${encodeURIComponent(id)}/clone`, {method: 'POST', body: {}}); toast('کپی ساخته شد'); renderTemplates(box); });
      if (a === 'delete') {
        const name = prompt('برای حذف، نام قالب را دقیقاً تایپ کنید'); if (!name) return;
        return busy(b, '…', async () => { await api(`/api/diag/server-templates/${encodeURIComponent(id)}/delete`, {method: 'POST', body: {confirm_name: name}}); toast('حذف شد'); renderTemplates(box); });
      }
    })));
  }
  async function editTpl(id, editable) {
    const [t, cat] = await Promise.all([api('/api/diag/server-templates/' + encodeURIComponent(id)), catalog()]);
    const dis = editable ? '' : 'disabled';
    modal(`<h3>${esc(t.name)}</h3><label class="small">نام<input id="stName" value="${esc(t.name)}" ${dis}></label>
      <label class="small">پلن<select id="stPlan" ${dis}>${cat.plans.map(p => `<option value="${esc(p.plan)}" ${p.plan === t.plan ? 'selected' : ''}>${esc(planLabel(p))}</option>`).join('')}</select></label>
      <label class="small">سیستم‌عامل<select id="stOs" class="ltr" ${dis}>${cat.images.map(i => `<option value="${esc(i.os)}" ${i.os === t.os ? 'selected' : ''}>${esc(i.os)}</option>`).join('')}</select></label>
      <label class="small">اسکریپت راه‌اندازی (bash، بدون رمز)<textarea id="stScript" class="ltr" style="min-height:160px;font-family:var(--mono)" ${dis}>${esc(t.init_script || '')}</textarea></label>
      ${editable ? '<div class="row"><button class="btn primary" id="stSave">ذخیره (نسخه‌ی جدید)</button></div>' : ''}
      ${(t.versions || []).length ? `<h4>نسخه‌ها</h4>${t.versions.map(v => `<div class="row small"><span>نسخه ${fa(v.version)} · ${esc(v.note || '')} · ${dt(v.ts)}</span>${v.version !== t.version ? `<button class="btn sm" data-restore="${v.version}">بازگردانی</button>` : ''}</div>`).join('')}` : ''}`);
    if (editable) $('#stSave').addEventListener('click', e => busy(e.currentTarget, '…', async () => {
      await api('/api/diag/server-templates/' + encodeURIComponent(id), {method: 'POST', body: {name: $('#stName').value, plan: $('#stPlan').value, os: $('#stOs').value, init_script: $('#stScript').value}});
      toast('ذخیره شد'); closeModal(); refreshTemplateBoxes();
    }));
    $$('[data-restore]').forEach(b => b.addEventListener('click', () => busy(b, '…', async () => {
      await api(`/api/diag/server-templates/${encodeURIComponent(id)}/restore`, {method: 'POST', body: {version: +b.dataset.restore}}); toast('بازگردانی شد'); closeModal(); refreshTemplateBoxes();
    })));
  }
  function refreshTemplateBoxes() { renderTemplates($('#srvTpls')); }

  /* templates page: append the server templates under the app templates (list view only) */
  const origTemplates = N.VIEWS.templates;
  if (origTemplates) N.VIEWS.templates = async arg => {
    await origTemplates(arg);
    if (arg || N.S.view !== 'templates') return;
    const v = $('#view'); const card = document.createElement('div'); card.className = 'card'; card.style.marginTop = '16px';
    card.innerHTML = '<h3>🖥️ قالب‌های سرور مجازی</h3><div class="muted small">سیستم‌عامل + پلن + اسکریپت راه‌اندازی؛ قیمت‌ها زنده از لیارا.</div><div id="srvTpls" style="margin-top:8px"><span class="spin"></span></div>';
    v.appendChild(card); renderTemplates($('#srvTpls'));
  };
})();
