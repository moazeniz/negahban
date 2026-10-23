'use strict';
/* Negahban extras: initial-setup wizard, first-run tour, daily report page, LLM usage / AI-mode / scaling / control-plane panels.
   CSP-safe (no inline handlers, no eval); every dynamic value is escaped with Ngh.esc. */
(function () {
  const N = window.Ngh, $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const {api, esc, fa, toast, modal, propCard, bindProps, VIEWS, TITLES} = N;
  const T = v => v == null ? '—' : fa(Math.round(v)) + ' تومان';
  const busy = async (b, label, fn) => { const t = b.innerHTML; b.disabled = true; b.innerHTML = '<span class="spin"></span> ' + label; try { return await fn(); } catch (e) { toast(e.message, 6000); } finally { b.disabled = false; b.innerHTML = t; } };
  Object.assign(TITLES, {wizard: 'راه‌اندازی اولیه', reports: 'گزارش روزانه'});

  /* ------------------------------------------------------------ initial setup wizard */
  const STEPS = ['منبع پروژه', 'تحلیل و پیشنهاد پلن', 'متغیرها', 'کارت تأیید', 'بیلد و استقرار', 'نتیجه و دامنه'];
  const stepbar = k => `<div class="steps-wiz">${STEPS.map((s, i) => `<span class="${i <= k ? 'on' : ''}">${fa(i + 1)}. ${s}</span>`).join('')}</div>`;
  VIEWS.wizard = async () => {
    const v = $('#view');
    v.innerHTML = `${stepbar(0)}<div class="card small muted" style="margin-bottom:12px">نگهبان پروژه را بررسی می‌کند، پلن مناسب را با قیمت واقعی لیارا پیشنهاد می‌دهد، برنامه را می‌سازد و مستقر می‌کند و سلامت آن را می‌سنجد. شما فقط کارهایی را انجام می‌دهید که از دست نگهبان برنمی‌آید (مثل وارد کردن مقادیر محرمانه یا ثبت رکورد دامنه در ایرنیک).</div>
      <div class="grid g2"><div class="card form"><h3>۱. منبع پروژه</h3>
        <label>آدرس مخزن عمومی git (https)<input id="wzGit" class="ltr" placeholder="https://github.com/user/repo"></label>
        <div class="muted small">یا</div><label>فایل zip پروژه<input type="file" id="wzZip" accept=".zip"></label>
        <button class="btn primary" id="wzGo">🔍 تحلیل پروژه</button><div class="muted small">از گفتگو هم می‌توانید بگویید: «این ریپو رو دیپلوی کن: …»</div></div>
        <div class="card" id="wzOut"><div class="empty">نتیجه‌ی تحلیل اینجا نمایش داده می‌شود</div></div></div>`;
    $('#wzGo').onclick = () => busy($('#wzGo'), 'در حال دریافت و تحلیل…', async () => {
      let a;
      if ($('#wzZip').files[0]) { const fd = new FormData(); fd.append('file', $('#wzZip').files[0]); a = await api('/api/projects/analyze', {method: 'POST', body: fd}); }
      else a = await api('/api/projects/analyze-git', {method: 'POST', body: {git_url: $('#wzGit').value.trim()}});
      analysis(a);
    });
  };
  function analysis(a) {
    const v = $('#view'); const plans = Object.entries(a.pricing || {});
    const sugg = (a.framework || 'app').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 16) || 'my-app';
    v.innerHTML = `${stepbar(2)}<div class="grid g2"><div class="card"><h3>۲. تحلیل و پیشنهاد</h3>
      <div class="facts"><div class="fact"><small>نوع پروژه</small>${esc(a.framework || '؟')}</div><div class="fact"><small>روش استقرار</small>${esc(a.platform)}${a.dockerfile_generated ? ' (Dockerfile ساخته شد)' : a.has_dockerfile ? ' (Dockerfile پروژه)' : ''}</div>
        <div class="fact"><small>پورت</small>${fa(a.port)}</div><div class="fact"><small>مسیر سلامت</small><span class="ltr">${esc(a.health_path || '/')}</span></div></div>
      <h3 style="margin-top:12px">چرا این پلن؟</h3><ul class="small">${(a.reasoning || []).map(r => `<li>${esc(r)}</li>`).join('')}</ul>
      ${(a.notes || []).map(n => `<div class="chip warn" style="margin:2px">${esc(n)}</div>`).join('')}
      <p><b>${esc(a.recommendation_fa)}</b></p>
      <div class="tablewrap"><table><tr><th>پلن</th><th>منابع</th><th>ساعتی</th><th>ماهانه</th></tr>${plans.map(([p, x]) => `<tr ${p === a.recommended_plan ? 'style="background:var(--brand-soft)"' : ''}><td><label class="switch"><input type="radio" name="wzPlan" value="${p}" ${p === a.recommended_plan ? 'checked' : ''}> ${esc(p)} · ${esc(x.name_fa)}</label></td><td>${fa(x.cpu)} هسته / ${fa(x.ram)} گیگ</td><td>${T(x.hour)}</td><td>${T(x.month)}</td></tr>`).join('')}</table></div></div>
      <form class="card form" id="wzForm"><h3>۳. نام برنامه و متغیرها</h3>
        <label>نام برنامه در لیارا (انگلیسی کوچک، خط تیره مجاز)<input id="wzApp" class="ltr" required pattern="[a-z0-9][a-z0-9-]{2,30}" value="${esc(sugg)}-${Math.random().toString(36).slice(2, 6)}"></label>
        <label>باندل<select id="wzBundle"><option value="basic">برنزی (basic) — ۲۰ استقرار در روز</option><option value="standard">نقره‌ای (standard) — استقرار بدون قطعی</option></select></label>
        ${(a.env_schema || []).length ? (a.env_schema || []).map(e => `<label class="ltr">${esc(e.name)} ${e.secret ? '🔒 (محرمانه؛ فقط روی سرور نگه‌داری می‌شود)' : ''}<input data-wzenv="${esc(e.name)}" data-sec="${e.secret ? 1 : 0}" ${e.secret ? 'type="password" autocomplete="off"' : ''}></label>`).join('') : '<div class="muted small">این پروژه متغیر محیطی لازم ندارد.</div>'}
        <button class="btn primary" type="submit">ساخت کارت تأیید راه‌اندازی</button><div id="wzCard" class="list"></div></form></div>`;
    $('#wzForm').onsubmit = e => { e.preventDefault(); busy($('#wzForm button[type=submit]'), 'در حال ساخت کارت…', async () => {
      const envs = {}, secrets = {};
      $$('[data-wzenv]').forEach(i => { if (i.value) (i.dataset.sec === '1' ? secrets : envs)[i.dataset.wzenv] = i.value; });
      const r = await api('/api/projects/launch', {method: 'POST', body: {project_id: a.project_id, app: $('#wzApp').value.trim(), plan: ($('input[name=wzPlan]:checked') || {}).value, bundle: $('#wzBundle').value, envs, secrets}});
      if (r.error) { toast(r.error, 7000); return; }
      const p = (await api('/api/proposals')).find(x => x.id === r.proposal.id);
      $('#wzCard').innerHTML = `${stepbar(3)}` + propCard(p); bindProps(); watch(p.id);
    }); };
  }
  function watch(pid) {  // refresh the card while it builds/verifies; the build log streams in via SSE op events
    let n = 0; const t = setInterval(async () => {
      if (++n > 200 || !document.querySelector(`[data-prop="${pid}"]`)) return clearInterval(t);
      const p = (await api('/api/proposals').catch(() => [])).find(x => x.id === pid); if (!p) return;
      if (['done', 'failed', 'unverified', 'rejected', 'expired', 'stale'].includes(p.status)) {
        clearInterval(t); const el = document.querySelector(`[data-prop="${pid}"]`); const logs = el && el.querySelector('[data-buildfor]');
        el.outerHTML = propCard(p); bindProps();
        const el2 = document.querySelector(`[data-prop="${pid}"]`); if (logs && el2) { const b = el2.querySelector('[data-buildfor]'); if (b) { b.innerHTML = logs.innerHTML; b.classList.remove('hidden'); } }
        if (p.status === 'done') { toast('برنامه با موفقیت راه‌اندازی و بررسی شد ✓', 6000); $('#wzCard').insertAdjacentHTML('afterbegin', stepbar(5)); }
      }
    }, 5000);
  }

  /* ------------------------------------------------------------ daily report */
  VIEWS.reports = async (rid) => {
    const v = $('#view'); const L = await api('/api/reports');
    const r = L.reports.length ? await api('/api/reports/' + (rid || 'latest')).catch(() => null) : null;
    v.innerHTML = `<div class="row" style="margin-bottom:12px"><button class="btn primary" id="rpGen">🗒️ ساخت گزارش همین حالا</button>
      <span class="muted small">گزارش خودکار هر روز ساعت ${fa(L.report_hour)} به وقت تهران ساخته می‌شود.</span>
      <select id="rpPick" class="right" style="width:auto">${L.reports.map(x => `<option value="${esc(x.id)}" ${r && r.id === x.id ? 'selected' : ''}>${esc(x.day)} · ${N.dt(x.created_at)}</option>`).join('')}</select></div>
      ${r ? render(r) : '<div class="empty card">هنوز گزارشی ساخته نشده است</div>'}`;
    $('#rpGen').onclick = () => busy($('#rpGen'), 'در حال جمع‌آوری…', async () => { const x = await api('/api/reports/generate', {method: 'POST'}); VIEWS.reports(x.id); });
    $('#rpPick') && ($('#rpPick').onchange = () => VIEWS.reports($('#rpPick').value));
  };
  function render(r) {
    const c = r.cost || {}, l = r.llm || {};
    return `<div class="card" style="margin-bottom:14px;background:var(--brand-soft)"><b>خلاصه‌ی ${esc(r.day)}:</b> ${esc(r.summary)}</div>
      <div class="kpis"><div class="kpi"><small>موجودی</small><b>${T(c.balance_toman)}</b></div><div class="kpi"><small>نرخ مصرف</small><b>${T(c.total_toman_per_hour)}</b><small>در ساعت</small></div>
        <div class="kpi"><small>کافی برای</small><b>${c.hours_left ? fa(Math.round(c.hours_left / 24 * 10) / 10) + ' روز' : '—'}</b></div><div class="kpi"><small>مدل زبانی امروز</small><b>${fa(l.calls || 0)} فراخوانی</b><small>≈ ${l.cost_usd != null ? '$' + l.cost_usd : '—'} · کش ${l.cache_hit_pct != null ? fa(l.cache_hit_pct) + '٪' : '—'}</small></div></div>
      <div class="card" style="margin-bottom:14px"><h3>پیشنهادها</h3><ul>${(r.recommendations || []).map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>
      <div class="card"><h3>برنامه‌ها (۲۴ ساعت گذشته)</h3><div class="tablewrap"><table><tr><th>برنامه</th><th>دسترس‌پذیری</th><th>پاسخ (میانگین / p95)</th><th>اوج CPU / حافظه</th><th>هزینه</th><th>رخدادها</th><th>اقدام خودران</th></tr>
        ${(r.apps || []).map(a => `<tr><td class="ltr">${esc(a.app)}${a.locked ? ' 🔒' : ''}</td><td>${a.uptime_pct != null ? fa(a.uptime_pct) + '٪' : '—'}</td><td>${fa(a.latency_avg_ms)} / ${fa(a.latency_p95_ms)} ms</td>
          <td>${a.cpu_peak_pct != null ? fa(a.cpu_peak_pct) + '٪' : '—'} / ${a.mem_peak_pct != null ? fa(a.mem_peak_pct) + '٪' : '—'}</td><td>${T(a.toman_per_hour)}/ساعت</td>
          <td class="small">${a.incidents.map(i => `<div>${esc(i.title)} <span class="muted">(${esc(i.status)})</span></div>`).join('') || '—'}</td><td class="small">${a.autopilot_actions.map(x => `<div>${esc(x.title)}</div>`).join('') || '—'}</td></tr>`).join('')}</table></div></div>`;
  }

  /* ------------------------------------------------------------ overview / autopilot / audit add-ons */
  const ov = VIEWS.overview;
  VIEWS.overview = async (silent) => {
    await ov(silent);
    if (N.S.view !== 'overview' || $('#xUsage')) return;
    const grid = document.createElement('div'); grid.className = 'grid g2'; grid.style.marginTop = '18px';
    grid.innerHTML = `<div class="card" id="xUsage"><h2>🧠 مصرف مدل زبانی</h2><div class="spin"></div></div><div class="card" id="xScale"><h2>↔️ مقیاس افقی (demo-shop)</h2><div class="spin"></div></div>`;
    $('#view').appendChild(grid);
    try {
      const [u, c] = await Promise.all([api('/api/llm/usage?days=7'), api('/api/control')]);
      const top = $('#topStats'); if (top && !c.ai_enabled) top.insertAdjacentHTML('afterbegin', '<span class="chip warn">⚙️ حالت بدون هوش مصنوعی</span>');
      if (!$('#xUsage')) return;
      $('#xUsage').innerHTML = `<h2>🧠 مصرف مدل زبانی</h2>${u.days.length ? `<div class="tablewrap"><table><tr><th>روز</th><th>فراخوانی</th><th>ورودی / خروجی</th><th>کش</th><th>هزینه‌ی تخمینی</th></tr>
        ${u.days.map(d => `<tr><td>${esc(d.day)}</td><td>${fa(d.calls)}</td><td>${fa(d.tokens_in + d.cache_read + d.cache_write)} / ${fa(d.tokens_out)}</td><td>${d.cache_hit_pct != null ? fa(d.cache_hit_pct) + '٪' : '—'}</td><td>$${esc(d.cost_usd)}</td></tr>`).join('')}</table></div>
        <div class="small muted">به تفکیک کاربرد (امروز): ${Object.entries((u.days[0] || {}).by_feature || {}).map(([k, x]) => `${esc(k)}: ${fa(x.calls)}`).join(' · ')}</div>` : '<div class="muted small">هنوز مصرفی ثبت نشده</div>'}`;
    } catch (e) { $('#xUsage') && ($('#xUsage').textContent = e.message); }
    try {
      const s = await api('/api/scaler'); const lb = s.lb || {}, sc = s.scaler || {};
      if (!$('#xScale')) return;
      $('#xScale').innerHTML = `<h2>↔️ مقیاس افقی (demo-shop)</h2>${lb.error && sc.error ? `<div class="muted small">کنترلر رپلیکا هنوز در دسترس نیست (${esc(lb.error)}).</div>` :
        `<div class="small">${(lb.backends || []).map(b => `<div><span class="dot ${b.healthy ? 'on' : 'bad'}"></span> <span class="ltr">${esc(b.name)}</span> · p95 ${fa(b.p95_ms)}ms · ${fa(b.requests)} درخواست</div>`).join('') || 'بک‌اندی ثبت نشده'}</div>
         <div class="small muted" style="margin-top:6px">آخرین تصمیم: ${esc(((sc.last_decision || {}).reason) || ((sc.last_decision || {}).action) || '—')} · کنترلر قطعی (بدون هوش مصنوعی) است؛ نگهبان فقط کارت پیشنهاد می‌سازد.</div>`}`;
    } catch (e) { $('#xScale') && ($('#xScale').innerHTML = '<h2>↔️ مقیاس افقی</h2><div class="muted small">در دسترس نیست</div>'); }
  };
  const ap = VIEWS.autopilot;
  VIEWS.autopilot = async (arg) => {
    await ap(arg);
    if (N.S.view !== 'autopilot') return;
    const [c, L, R] = await Promise.all([api('/api/control'), api('/api/llm'), api('/api/reports')]);
    const box = document.createElement('div'); box.className = 'card form'; box.style.marginBottom = '16px';
    box.innerHTML = `<h3>⚙️ هوش مصنوعی و گزارش</h3>
      <label class="switch"><input type="checkbox" id="xAi" ${c.ai_enabled ? 'checked' : ''}> هوش مصنوعی فعال باشد (اگر خاموش شود: پایش، مقیاس خودکار، ری‌استارت، بررسی سلامت، کارت‌ها، صفحه‌ی وضعیت و گزارش روزانه بدون مدل زبانی ادامه می‌یابند)</label>
      <label class="switch"><input type="checkbox" id="xCache" ${L.prompt_cache ? 'checked' : ''}> کش پرامپت (کاهش هزینه‌ی ورودی مدل)</label>
      <div class="row"><label>ساعت گزارش روزانه (تهران)<input id="xHour" type="number" min="0" max="23" value="${R.report_hour}" style="width:90px"></label><button class="btn sm" id="xHourSave">ذخیره</button></div>`;
    $('#view').prepend(box);
    $('#xAi').onchange = async e => { try { await api('/api/control/ai', {method: 'PUT', body: {enabled: e.target.checked}}); toast(e.target.checked ? 'هوش مصنوعی فعال شد' : 'حالت بدون هوش مصنوعی فعال شد'); } catch (er) { toast(er.message); } };
    $('#xCache').onchange = async e => { try { await api('/api/llm/cache', {method: 'PUT', body: {enabled: e.target.checked}}); toast('ذخیره شد'); } catch (er) { toast(er.message); } };
    $('#xHourSave').onclick = () => busy($('#xHourSave'), '…', async () => { await api('/api/reports/hour', {method: 'PUT', body: {hour: +$('#xHour').value}}); toast('ذخیره شد'); });
  };
  const au = VIEWS.audit;
  VIEWS.audit = async () => {
    await au();
    const c = await api('/api/control').catch(() => null); if (!c || N.S.view !== 'audit') return;
    const box = document.createElement('div'); box.className = 'card'; box.style.marginBottom = '16px';
    const m = c.memory || {};
    box.innerHTML = `<h3>🛡️ سلامت خود نگهبان</h3><div class="row small">${Object.entries(c.loops).map(([k, x]) => `<span class="chip ${x.running ? 'ok' : 'bad'}">${esc(k)}${x.restarts ? ' · ری‌استارت ' + fa(x.restarts) : ''}</span>`).join('')}
      <span class="chip ${c.shed ? 'warn' : 'ok'}">حافظه: ${m.used_mb != null ? fa(m.used_mb) + (m.limit_mb ? ' از ' + fa(m.limit_mb) : '') + ' MB' : '—'}${c.shed ? ' · کاهش بار' : ''}</span>
      <span class="chip ${c.ai_enabled ? 'info' : 'warn'}">${c.ai_enabled ? '🧠 AI فعال' : '⚙️ بدون AI'}</span></div>
      <div class="small" style="margin-top:8px">📦 اسنپ‌شات پایگاه داده: ${c.snapshot_last ? `آخرین ${N.ago(c.snapshot_last.ts)} (${esc(c.snapshot_last.file)}، ${fa(Math.round(c.snapshot_last.bytes / 1024))} KB)` : 'هنوز ساخته نشده'} · ${fa(c.snapshots.length)} نسخه‌ی نگهداری‌شده
      <button class="btn sm" id="xSnap">اسنپ‌شات همین حالا</button></div>`;
    $('#view').prepend(box);
    $('#xSnap').onclick = () => busy($('#xSnap'), '…', async () => { const r = await api('/api/snapshots', {method: 'POST'}); toast('ساخته شد: ' + r.file); });
  };


  /* ------------------------------------------------------------ alerts: rules, notifications, channels, guardrails, bell */
  Object.assign(TITLES, {alerts: 'قوانین هشدار و اعلان‌ها'});
  const SEVFA = {info: 'اطلاع', warning: 'هشدار', critical: 'بحرانی'}, STFA = {open: 'باز', acked: 'دیده شد', resolved: 'برطرف شد'};
  const ACTFA = {notify: 'فقط اعلان', wake: 'بیدار کردن نگهبان (تشخیص خودکار)', 'autopilot:restart_app': 'ری‌استارت خودکار (طبق سیاست)'};
  VIEWS.alerts = async () => {
    const v = $('#view');
    const [R, Nn, C, G, apps] = await Promise.all([api('/api/alerts/rules'), api('/api/notifications'), api('/api/alerts/channels'), api('/api/guardrails'), api('/api/policies')]);
    const ruleRow = r => `<tr><td><b>${esc(r.name)}</b><div class="muted small ltr">${r.kind === 'log' ? esc(r.pattern) : `${esc(r.metric)} ${esc(r.op)} ${esc(r.threshold)} · ${fa(r.window_min)}′/${fa(r.min_duration_min)}′`}</div></td>
      <td><span class="chip ${r.severity === 'critical' ? 'bad' : r.severity === 'warning' ? 'warn' : 'info'}">${esc(SEVFA[r.severity])}</span></td><td class="small">${esc(ACTFA[r.action] || r.action)}</td>
      <td class="small">${esc(r.scope)} · خنک‌سازی ${fa(r.cooldown_min)}′${r.quiet_hours ? ' · سکوت ' + esc(r.quiet_hours) : ''}</td><td>${r.enabled ? '✅' : '⏸️'} <span class="muted small">v${fa(r.version)}</span></td>
      <td><button class="btn sm" data-redit="${esc(r.id)}">ویرایش</button></td></tr>`;
    v.innerHTML = `<div class="card small muted" style="margin-bottom:12px">قوانین هشدار کاملاً قطعی‌اند (بدون هوش مصنوعی): هر دقیقه روی متریک‌ها و لاگ‌ها، و فوراً روی رویدادهای ارسالی برنامه‌ها اجرا می‌شوند. اقدام «بیدار کردن» یک رخداد باز می‌کند تا نگهبان علت را تحلیل کند.</div>
      <div class="card" style="margin-bottom:16px"><div class="row"><h3 style="margin:0">🔔 اعلان‌ها</h3><button class="btn sm right" id="nPerm">فعال کردن اعلان مرورگر</button><button class="btn sm" id="nTestDash">اعلان آزمایشی</button></div>
        <div class="list" style="margin-top:8px">${Nn.slice(0, 30).map(n => `<div class="item"><div class="row"><span class="chip ${n.severity === 'critical' ? 'bad' : 'warn'}">${esc(SEVFA[n.severity] || n.severity)}${n.level ? ' · یادآوری ' + fa(n.level) : ''}</span><b>${esc(n.title)}</b>
          <span class="chip ${n.status === 'open' ? 'bad' : n.status === 'acked' ? 'warn' : 'ok'}">${esc(STFA[n.status] || n.status)}</span><span class="muted small right">${N.ago(n.ts)}</span></div><div class="small">${esc(n.body)}</div>
          <div class="row">${n.link ? `<a class="btn sm ghost" href="${esc(n.link)}">مشاهده</a>` : ''}${n.status === 'open' ? `<button class="btn sm" data-nack="${esc(n.id)}">دیدم</button>` : ''}${n.status !== 'resolved' ? `<button class="btn sm" data-nres="${esc(n.id)}">برطرف شد</button>` : ''}</div></div>`).join('') || '<div class="muted small">اعلانی نیست</div>'}</div></div>
      <div class="card" style="margin-bottom:16px"><div class="row"><h3 style="margin:0">قوانین</h3><button class="btn sm primary right" id="rNew">+ قانون جدید</button></div>
        <div class="tablewrap"><table><tr><th>قانون</th><th>شدت</th><th>اقدام</th><th>دامنه</th><th>وضعیت</th><th></th></tr>${R.rules.map(ruleRow).join('')}</table></div></div>
      <div class="grid g2"><form class="card form" id="chForm"><h3>کانال‌های اعلان (تا تنظیم نشوند غیرفعال‌اند)</h3>
        <label>ایمیل گیرنده (سرویس ایمیل لیارا)<input name="email_to" class="ltr" value="${esc(C.email.to)}" placeholder="owner@example.ir"></label>
        <label>شناسه‌ی سرویس ایمیل لیارا<input name="mail_server_id" class="ltr" value="${esc(C.email.mail_server_id)}"></label>
        <label>توکن ربات بله ${C.bale.token_set ? '(تنظیم شده)' : ''}<input name="bale_token" type="password" class="ltr" autocomplete="off"></label>
        <label>شناسه‌ی گفتگوی بله<input name="bale_chat" class="ltr" value="${esc(C.bale.chat_id)}"></label>
        <label>وب‌هوک<input name="webhook_url" class="ltr" value="${esc(C.webhook.url)}" placeholder="https://…"></label>
        <div class="row"><button class="btn primary" type="submit">ذخیره</button>${['email', 'bale', 'webhook'].map(c => `<button type="button" class="btn sm" data-chtest="${c}">آزمایش ${c}${C[c].enabled ? ' ✅' : ''}</button>`).join('')}</div></form>
      <form class="card form" id="gForm"><h3>محدودیت‌های خود نگهبان</h3>
        <label>سقف هزینه‌ی روزانه‌ی مدل زبانی (دلار)<input name="llm_daily_cap_usd" type="number" step="0.1" min="0" value="${esc(G.llm_daily_cap_usd)}"></label>
        <label>سقف برای گفتگو / خودترمیمی / تشخیص (دلار)<input name="caps" class="ltr" value="${esc(Object.entries(G.llm_feature_caps_usd || {}).map(([k, x]) => k + '=' + x).join(' '))}"></label>
        <label>حداکثر اقدام خودکار در ساعت<input name="max_auto_actions_per_hour" type="number" min="0" value="${esc(G.max_auto_actions_per_hour)}"></label>
        <div class="small muted">با رسیدن به سقف هزینه، نگهبان خودکار به «حالت بدون هوش مصنوعی» می‌رود و اعلان می‌دهد؛ پایش و خودران قاعده‌محور ادامه دارد.${G.ai_off_reason ? ' آخرین علت: ' + esc(G.ai_off_reason) : ''}</div>
        <button class="btn primary" type="submit">ذخیره</button>
        <h3 style="margin-top:12px">ارسال فوری رویداد از برنامه‌ها</h3><div class="small">برنامه‌ها می‌توانند رویدادهای مهم (کرش، کار مرده، خطای مدل) را فوراً به <code>POST /api/ingest/event</code> بفرستند. توکن هر برنامه:</div>
        <div class="row">${Object.keys(apps).filter(n => apps[n].role !== 'off').map(n => `<button type="button" class="btn sm" data-itok="${esc(n)}">توکن ${esc(n)}</button>`).join('')}</div><div id="iTok" class="mono small"></div></form></div>`;
    $$('[data-nack]').forEach(b => b.onclick = () => busy(b, '…', async () => { await api(`/api/notifications/${b.dataset.nack}/ack`, {method: 'POST'}); VIEWS.alerts(); }));
    $$('[data-nres]').forEach(b => b.onclick = () => busy(b, '…', async () => { await api(`/api/notifications/${b.dataset.nres}/resolve`, {method: 'POST'}); VIEWS.alerts(); }));
    $('#nPerm').onclick = async () => { if (!('Notification' in window)) return toast('مرورگر از اعلان پشتیبانی نمی‌کند'); const p = await Notification.requestPermission(); toast(p === 'granted' ? 'اعلان مرورگر فعال شد' : 'اجازه داده نشد'); };
    $('#nTestDash').onclick = () => busy($('#nTestDash'), '…', async () => { await api('/api/alerts/channels/dashboard/test', {method: 'POST'}); });
    $$('[data-chtest]').forEach(b => b.onclick = () => busy(b, '…', async () => { const r = await api(`/api/alerts/channels/${b.dataset.chtest}/test`, {method: 'POST'}); toast(JSON.stringify(r)); }));
    $('#chForm').onsubmit = e => { e.preventDefault(); const f = new FormData(e.target), body = {}; for (const [k, x] of f.entries()) if (k !== 'bale_token' || x) body[k] = x; busy(e.target.querySelector('[type=submit]'), '…', async () => { await api('/api/alerts/channels', {method: 'PUT', body}); toast('ذخیره شد'); VIEWS.alerts(); }); };
    $('#gForm').onsubmit = e => { e.preventDefault(); const f = new FormData(e.target); const caps = {}; String(f.get('caps') || '').split(/\s+/).filter(Boolean).forEach(t => { const [k, x] = t.split('='); if (k && x) caps[k] = +x; });
      busy(e.target.querySelector('[type=submit]'), '…', async () => { await api('/api/guardrails', {method: 'PUT', body: {llm_daily_cap_usd: +f.get('llm_daily_cap_usd'), llm_feature_caps_usd: caps, max_auto_actions_per_hour: +f.get('max_auto_actions_per_hour')}}); toast('ذخیره شد'); }); };
    $$('[data-itok]').forEach(b => b.onclick = () => busy(b, '…', async () => { const r = await api(`/api/alerts/ingest-token/${b.dataset.itok}`); $('#iTok').textContent = `${r.app}: Authorization: Bearer ${r.token}`; }));
    const edit = r => { modal(`<h2>${r ? 'ویرایش قانون' : 'قانون جدید'}</h2><div class="small muted">نوع metric: معیار، عملگر و آستانه. نوع log: الگو (کلمه، <code>re:عبارت‌منظم</code> یا <code>event=app.crash level=critical</code>).</div>
        <textarea id="rJson" class="mono" style="height:300px;direction:ltr">${esc(JSON.stringify(r || {name: 'قانون جدید', kind: 'log', pattern: 'event=app.crash', scope: '*', severity: 'warning', action: 'notify', cooldown_min: 10, quiet_hours: '', enabled: true}, null, 2))}</textarea>
        <div class="small muted ltr">metrics: ${Object.keys(R.metrics).join(', ')} · actions: notify, wake, autopilot:restart_app</div>
        <div class="row" style="margin-top:8px"><button class="btn primary" id="rSave">ذخیره (نسخه‌ی جدید)</button>${r ? `<button class="btn" id="rVers">نسخه‌ها</button><button class="btn danger" id="rDel">حذف</button>` : ''}</div><div id="rOut" class="small"></div>`);
      $('#rSave').onclick = () => busy($('#rSave'), '…', async () => { let body; try { body = JSON.parse($('#rJson').value); } catch (er) { $('#rOut').textContent = 'JSON نامعتبر: ' + er.message; return; }
        try { await api('/api/alerts/rules', {method: 'POST', body}); $('#modal').classList.add('hidden'); toast('ذخیره شد'); VIEWS.alerts(); } catch (er) { $('#rOut').textContent = er.message; } });
      r && ($('#rVers').onclick = async () => { const vs = await api(`/api/alerts/rules/${r.id}/versions`); $('#rOut').innerHTML = vs.map(x => `<div>v${fa(x.version)} · ${esc(x.actor)} · ${N.dt(x.ts)}</div>`).join(''); });
      r && ($('#rDel').onclick = () => { if (confirm('این قانون حذف شود؟')) busy($('#rDel'), '…', async () => { await api(`/api/alerts/rules/${r.id}`, {method: 'DELETE'}); $('#modal').classList.add('hidden'); VIEWS.alerts(); }); });
    };
    $('#rNew').onclick = () => edit(null);
    $$('[data-redit]').forEach(b => b.onclick = () => edit(R.rules.find(x => x.id === b.dataset.redit)));
  };
  // bell + browser notifications from the live stream
  function bell() {
    const top = $('#topStats'); if (!top || $('#xBell')) return;
    const b = document.createElement('a'); b.id = 'xBell'; b.className = 'chip'; b.href = '#alerts'; b.textContent = '🔔'; top.prepend(b);
    api('/api/notifications?status=open').then(L => { if (L.length) { b.textContent = '🔔 ' + fa(L.length); b.classList.add('bad'); } }).catch(() => 0);
  }
  setInterval(bell, 3000);
  const es0 = window.EventSource; let hooked = false;
  setInterval(() => {  // piggy-back on the app's EventSource for 'notify' events
    const es = N.S.es; if (!es || es._nghNotify) return; es._nghNotify = true;
    es.addEventListener('message', m => { let ev; try { ev = JSON.parse(m.data); } catch { return; } if (ev.kind !== 'notify' || !ev.notification) return;
      const n = ev.notification; if (n.status && n.status !== 'open' && n.status !== 'test') return;
      toast(`🔔 ${n.title}`, 8000); const bb = $('#xBell'); if (bb) { bb.remove(); bell(); }
      try { if ('Notification' in window && Notification.permission === 'granted') new Notification('نگهبان: ' + n.title, {body: n.body || '', tag: n.id}); } catch { } });
  }, 2000);

  /* ------------------------------------------------------------ first-run tour */
  const TOUR = [
    ['🛡️ نگهبان چیست؟', 'نگهبان همکار فنی شماست: سایت‌هایتان را روی لیارا شبانه‌روز پایش می‌کند، علت خطاها را پیدا می‌کند، راه‌حل پیشنهاد می‌دهد و با اجازه‌ی شما اجرا و سپس بررسی می‌کند.', 'overview'],
    ['🏠 داشبورد', 'هر کارت یک برنامه است: وضعیت، پلن و هزینه، مصرف CPU و حافظه و زمان پاسخ. رخدادها، هزینه‌ی تیم و عملیات زنده هم همین‌جاست.', 'overview'],
    ['💬 گفتگو', 'به زبان ساده بپرسید: «چرا سایتم کند است؟»، «نمودار حافظه را نشان بده»، «این ریپو را دیپلوی کن». مراحل کار نگهبان را زنده می‌بینید.', 'chat'],
    ['✅ کارت‌های تأیید و امنیت', 'هیچ کار حساسی بدون تأیید شما انجام نمی‌شود. هر کارت قطعی، هزینه، ریسک و راه بازگشت را نشان می‌دهد و پس از اجرا نتیجه را راستی‌آزمایی می‌کند.', 'approvals'],
    ['🤖 خودران و بودجه', 'اینجا تعیین می‌کنید نگهبان بدون پرسیدن چه کند: مثلاً ارتقا یا کاهش پلن در بازه و سقف هزینه‌ی روزانه‌ای که شما تعیین می‌کنید. می‌توانید هوش مصنوعی را هم خاموش کنید؛ پایش و خودران قاعده‌محور ادامه می‌یابند.', 'autopilot'],
    ['📊 صفحه‌ی وضعیت و گزارش روزانه', 'صفحه‌ی عمومی /status دسترس‌پذیری را از دو نقطه نشان می‌دهد و هر روز صبح گزارش سلامت، هزینه و پیشنهادها آماده می‌شود. هر وقت خواستید از دکمه‌ی «راهنما» دوباره این معرفی را ببینید.', 'reports'],
  ];
  async function tour(i = 0) {
    if (i >= TOUR.length) { $('#xTour') && $('#xTour').remove(); api('/api/prefs', {method: 'PUT', body: {tour: 'completed', tour_ts: Date.now() / 1000}}).catch(() => 0); try { localStorage.setItem('ngh_tour', 'completed'); } catch { } return; }
    const [t, body, view] = TOUR[i]; N.go(view);
    let o = $('#xTour'); if (!o) { o = document.createElement('div'); o.id = 'xTour'; o.className = 'tour'; document.body.appendChild(o); }
    o.innerHTML = `<div class="tour-card" role="dialog" aria-label="${esc(t)}"><div class="muted small">${fa(i + 1)} از ${fa(TOUR.length)}</div><h3>${esc(t)}</h3><p>${esc(body)}</p>
      <div class="row"><button class="btn ghost sm" id="tSkip">رد کردن</button>${i ? '<button class="btn sm" id="tPrev">قبلی</button>' : ''}<button class="btn primary right" id="tNext">${i === TOUR.length - 1 ? 'پایان' : 'بعدی'}</button></div></div>`;
    $('#tNext').onclick = () => tour(i + 1); $('#tPrev') && ($('#tPrev').onclick = () => tour(i - 1));
    $('#tSkip').onclick = () => { o.remove(); api('/api/prefs', {method: 'PUT', body: {tour: 'skipped', tour_ts: Date.now() / 1000}}).catch(() => 0); try { localStorage.setItem('ngh_tour', 'skipped'); } catch { } };
  }
  N.tour = tour;
  function helpButton() {
    const foot = $('.side-foot'); if (foot && !$('#xHelp')) { const b = document.createElement('button'); b.id = 'xHelp'; b.className = 'link'; b.textContent = 'راهنما'; b.onclick = () => tour(0); foot.insertBefore(b, foot.firstChild); }
    const top = $('.top'); if (top && !$('#xHelp2')) { const b = document.createElement('button'); b.id = 'xHelp2'; b.className = 'btn sm ghost'; b.textContent = '❔ راهنما'; b.onclick = () => tour(0); top.appendChild(b); }
  }
  async function maybeStartTour() {
    helpButton();
    let st = null; try { st = localStorage.getItem('ngh_tour'); } catch { }
    if (!st) { const p = await api('/api/prefs').catch(() => ({})); st = p.tour; }
    if (!st && !$('#app').classList.contains('hidden')) tour(0);
  }
  const startWhenLoggedIn = setInterval(() => { if (!$('#app').classList.contains('hidden')) { clearInterval(startWhenLoggedIn); maybeStartTour(); } }, 1000);
})();
