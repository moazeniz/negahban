'use strict';
/* Negahban dashboard — vanilla JS, Persian RTL. All server text is escaped before insertion. */
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fa = n => (n === null || n === undefined || n === '') ? '—' : Number(n).toLocaleString('fa-IR');
const pct = v => v == null ? '—' : fa(Math.round(v * 100)) + '٪';
const toman = v => v == null ? '—' : fa(Math.round(v)) + ' تومان';
const ago = ts => { if (!ts) return '—'; const s = Math.max(0, Date.now() / 1000 - ts); return s < 60 ? 'لحظاتی پیش' : s < 3600 ? fa(Math.floor(s / 60)) + ' دقیقه پیش' : s < 86400 ? fa(Math.floor(s / 3600)) + ' ساعت پیش' : fa(Math.floor(s / 86400)) + ' روز پیش'; };
const left = ts => { const s = Math.max(0, ts - Date.now() / 1000); return s < 60 ? 'کمتر از یک دقیقه' : fa(Math.ceil(s / 60)) + ' دقیقه'; };
const clock = ts => new Date(ts * 1000).toLocaleTimeString('fa-IR', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
const dt = ts => new Date(ts * 1000).toLocaleString('fa-IR', {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
const PLAN_FA = {'small-g2': 'زمین', 'medium-g2': 'مریخ', 'standard-base-g2': 'مشتری', 'standard-plus-g2': 'زحل', 'pro-g2': 'اورانوس', 'pro-plus-g2': 'نپتون'};
const ROLE_FA = {demo: 'نمایشی (سورس در نگهبان)', monitored: 'پایش', self: 'خود نگهبان', off: 'غیرفعال'};
const STATUS_FA = {pending: 'در انتظار تأیید', executing: 'در حال اجرا', verifying: 'در حال راستی‌آزمایی', done: 'انجام و تأیید شد', failed: 'ناموفق', rejected: 'رد شد',
  expired: 'منقضی شد', stale: 'باطل (وضعیت تغییر کرد)', unverified: 'اجرا شد، راستی‌آزمایی ناموفق'};
const ORIGIN_FA = {chat: 'گفتگو', orchestrator: 'پایشگر خودکار', autopilot: 'خودران', mcp: 'MCP', wizard: 'ویزارد', dashboard: 'داشبورد', scenario: 'سناریو'};
const INC_FA = {open: 'باز', mitigating: 'در حال رفع', resolved: 'برطرف شد'};
let S = {csrf: null, view: 'overview', ov: null, session: localStorage.getItem('ngh_session') || ('s' + Math.random().toString(36).slice(2, 10)), chatSteps: [], busy: false, es: null};
localStorage.setItem('ngh_session', S.session);
const getCookie = n => (document.cookie.split('; ').find(c => c.startsWith(n + '=')) || '').split('=')[1];

async function api(path, opts = {}) {
  const o = {method: opts.method || 'GET', headers: {}, credentials: 'same-origin'};
  if (opts.body instanceof FormData) o.body = opts.body;
  else if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers['Content-Type'] = 'application/json'; }
  if (o.method !== 'GET') o.headers['X-CSRF-Token'] = getCookie('ngh_csrf') || S.csrf || '';
  const r = await fetch(path, o);
  if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('ابتدا وارد شوید'); }
  let data = null; try { data = await r.json(); } catch { }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || ('خطای ' + r.status));
  return data;
}
function toast(msg, ms = 3800) { const t = $('#toast'); t.textContent = msg; t.classList.remove('hidden'); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add('hidden'), ms); }
function modal(html) { $('#modalBody').innerHTML = html; $('#modal').classList.remove('hidden'); }
$('#modalX').onclick = () => $('#modal').classList.add('hidden');
$('#modal').onclick = e => { if (e.target.id === 'modal') $('#modal').classList.add('hidden'); };

/* ---------------------------------------------------------------- auth */
function showLogin() { $('#login').classList.remove('hidden'); $('#app').classList.add('hidden'); if (S.es) { S.es.close(); S.es = null; } }
$('#loginForm').onsubmit = async e => {
  e.preventDefault(); $('#loginErr').textContent = '';
  try { const r = await api('/api/login', {method: 'POST', body: {password: $('#pw').value}}); S.csrf = r.csrf; $('#pw').value = ''; boot(); }
  catch (err) { $('#loginErr').textContent = err.message; }
};
$('#logout').onclick = async () => { await api('/api/logout', {method: 'POST'}).catch(() => 0); showLogin(); };

/* ---------------------------------------------------------------- routing */
const TITLES = {overview: 'داشبورد', chat: 'گفتگو با نگهبان', approvals: 'تأییدها', incidents: 'رخدادها', autopilot: 'خودران و بودجه', wizard: 'پروژه‌ی جدید',
  domains: 'دامنه‌ی اختصاصی', templates: 'قالب‌های استقرار', terminal: 'ترمینال و عیب‌یابی', logs: 'کاوشگر لاگ', memory: 'حافظه‌ی نگهبان', servers: 'سرورهای مجازی', scenarios: 'سناریوهای نمایشی', audit: 'گزارش عملیات', limits: 'محدودیت‌های API لیارا', app: 'جزئیات برنامه', more: 'بیشتر'};
function go(view, arg) {
  S.view = view; S.arg = arg; location.hash = view + (arg ? '/' + arg : '');
  $$('#nav a, #tabbar a').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  $('#viewTitle').textContent = TITLES[view] || '';
  (VIEWS[view] || VIEWS.overview)(arg);
}
$$('#nav a, #tabbar a').forEach(a => a.onclick = () => go(a.dataset.view));
window.addEventListener('hashchange', () => { const [v, a] = location.hash.slice(1).split('/'); if (v && (v !== S.view || a !== S.arg)) go(v, a); });

/* ---------------------------------------------------------------- live stream */
function connect() {
  if (S.es) S.es.close();
  const es = new EventSource('/api/stream'); S.es = es;
  es.onopen = () => { S.sseErr = 0; $('#liveDot').className = 'dot on'; $('#liveText').textContent = 'زنده'; };
  es.onerror = () => { S.sseErr = (S.sseErr || 0) + 1; $('#liveDot').className = 'dot warn'; $('#liveText').textContent = S.sseErr > 3 ? 'به‌روزرسانی دوره‌ای' : 'اتصال دوباره…'; };
  es.onmessage = m => { let ev; try { ev = JSON.parse(m.data); } catch { return; } onEvent(ev); };
}
function onEvent(ev) {
  if (ev.kind === 'tick') { if (S.view === 'overview') refreshOverviewSoon(); return; }
  if (ev.kind === 'proposal') {
    const p = ev.proposal; refreshCounts();
    if (p.status === 'pending' && p.origin !== 'chat') toast('🔔 کارت تأیید جدید: ' + p.title);
    if (S.view === 'approvals') VIEWS.approvals(); if (S.view === 'overview') refreshOverviewSoon();
    $$(`[data-prop="${p.id}"]`).forEach(el => el.outerHTML = propCard(p, true)); bindProps();
    return;
  }
  if (ev.kind === 'incident') { refreshCounts(); if (ev.incident && ev.incident.status === 'open') toast('🚨 ' + ev.incident.app + ': ' + ev.incident.title); if (S.view === 'incidents') VIEWS.incidents(); if (S.view === 'overview') refreshOverviewSoon(); return; }
  if (ev.kind === 'op') { opLine(ev); return; }
  if (ev.session && ev.session === S.session) chatEvent(ev);
}
function opLine(ev) {
  const box = $('#opsFeed'); if (box) { box.insertAdjacentHTML('afterbegin', `<div><time>${clock(ev.ts)}</time>${esc(ev.app || '')} — ${esc(ev.text)}</div>`); while (box.children.length > 30) box.lastChild.remove(); }
  $$(`[data-opfor="${ev.proposal_id}"]`).forEach(el => el.textContent = ev.text);
  if (ev.build_lines) $$(`[data-buildfor="${ev.proposal_id}"]`).forEach(el => { el.classList.remove('hidden'); ev.build_lines.forEach(l => el.insertAdjacentHTML('beforeend', `<div>${esc(l)}</div>`)); while (el.children.length > 60) el.firstChild.remove(); el.scrollTop = el.scrollHeight; });
}
let _ovT = null; function refreshOverviewSoon() { clearTimeout(_ovT); _ovT = setTimeout(() => S.view === 'overview' && VIEWS.overview(true), 1200); }
async function refreshCounts() {
  try { const [p, i] = await Promise.all([api('/api/proposals?status=pending'), api('/api/incidents')]);
    const n = p.length, m = i.filter(x => x.status !== 'resolved').length;
    for (const id of ['#navPending', '#tabPending']) { $(id).textContent = fa(n); $(id).classList.toggle('hidden', !n); }
    $('#navInc').textContent = fa(m); $('#navInc').classList.toggle('hidden', !m);
  } catch { }
}

/* ---------------------------------------------------------------- tiny charts */
function spark(vals, color = '#0f766e', max = 1) {
  if (!vals || vals.length < 2) return '<svg class="spark"></svg>';
  const w = 200, h = 34, mx = Math.max(max, ...vals) || 1, step = w / (vals.length - 1);
  const pts = vals.map((v, i) => `${(w - i * step).toFixed(1)},${(h - 2 - (v / mx) * (h - 4)).toFixed(1)}`).join(' ');
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polyline fill="none" stroke="${color}" stroke-width="1.8" points="${pts}"/></svg>`;
}
function lineChart(series, limit, unit, color = '#0f766e') {
  if (!series || series.length < 2) return '<div class="empty small">داده‌ای برای نمودار نیست</div>';
  const w = 600, h = 150, t0 = series[0][0], t1 = series[series.length - 1][0], mx = Math.max(limit || 0, ...series.map(s => s[1])) * 1.1 || 1;
  const X = t => w - ((t - t0) / ((t1 - t0) || 1)) * w, Y = v => h - 4 - (v / mx) * (h - 10);
  const pts = series.map(([t, v]) => `${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
  const lim = limit ? `<line x1="0" x2="${w}" y1="${Y(limit)}" y2="${Y(limit)}" stroke="#ef4444" stroke-dasharray="5 4"/><text x="4" y="${Y(limit) - 4}" font-size="11" fill="#ef4444">سقف پلن ${fa(limit)} ${unit}</text>` : '';
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><polygon fill="${color}18" points="${w},${h} ${pts} 0,${h}"/><polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}"/>${lim}</svg>
  <div class="legend row"><span>${dt(t0)}</span><span class="right">${dt(t1)}</span></div>`;
}
const barCls = v => v >= .85 ? 'bar bad' : v >= .65 ? 'bar warn' : 'bar';

/* ---------------------------------------------------------------- views */
const VIEWS = {};
VIEWS.overview = async (silent) => {
  const v = $('#view'); if (!silent) v.innerHTML = '<div class="empty">در حال بارگذاری…</div>';
  let ov; try { ov = await api('/api/overview'); } catch (e) { v.innerHTML = `<div class="card err">${esc(e.message)}</div>`; return; }
  S.ov = ov; if (S.view !== 'overview') return;
  const open = ov.incidents.filter(i => i.status !== 'resolved');
  const orch = ov.orchestrator, wd = ov.watchdog;
  $('#topStats').innerHTML = `<span class="chip ${orch.last_cycle && Date.now() / 1000 - orch.last_cycle < 180 ? 'ok' : 'warn'}">🛰️ پایشگر: ${orch.last_cycle ? ago(orch.last_cycle) : 'شروع نشده'}</span>
    <span class="chip ${wd && Date.now() / 1000 - wd.ts < 900 ? 'ok' : 'warn'}">🐕 نگهبانِ نگهبان: ${wd ? ago(wd.ts) : 'گزارشی نیامده'}</span>
    ${ov.llm ? `<span class="chip info" title="ارائه‌دهنده‌ی فعال مدل زبانی">🧠 ${esc(ov.llm.provider === 'anthropic' ? 'Anthropic' : 'BoxAPI')} · <span class="ltr">${esc(ov.llm.model)}</span></span>` : ''}`;
  const apps = ov.apps.filter(a => a.role !== 'off');
  v.innerHTML = `
  <div class="kpis">
    <div class="kpi"><small>برنامه‌های تحت پایش</small><b>${fa(apps.length)}</b></div>
    <div class="kpi"><small>رخدادهای باز</small><b class="${open.length ? 'sev-high' : ''}">${fa(open.length)}</b></div>
    <div class="kpi"><small>منتظر تأیید شما</small><b>${fa(ov.pending.length)}</b></div>
    <div class="kpi"><small>هزینه‌ی جاری همه‌ی برنامه‌ها</small><b>${toman(ov.burn_toman_h)}</b><small>در ساعت (≈ ${toman(ov.burn_toman_h * 24)} در روز)</small></div>
    <div class="kpi"><small>موجودی تیم</small><b id="kpiBal">${ov.orchestrator.balance ? toman(ov.orchestrator.balance.balance_toman) : '—'}</b><small>${ov.orchestrator.balance && ov.orchestrator.balance.hours_left ? 'کافی برای ~' + fa(ov.orchestrator.balance.hours_left) + ' ساعت' : ''}</small></div>
  </div>
  ${ov.pending.length ? `<div class="sec-title"><h2>⏳ منتظر تأیید شما</h2><a class="right" data-act="go" data-arg="approvals">همه</a></div><div class="list">${ov.pending.slice(0, 3).map(p => propCard(p)).join('')}</div>` : ''}
  <div class="sec-title"><h2>برنامه‌ها</h2><span class="grow"></span>${ov.unregistered.length ? `<span class="chip">${fa(ov.unregistered.length)} برنامه‌ی دیگر در تیم</span>` : ''}</div>
  <div class="grid g3">${apps.map(appCard).join('') || '<div class="empty">برنامه‌ای ثبت نشده</div>'}</div>
  <div class="grid g2" style="margin-top:18px">
    <div class="card"><h2>🚨 رخدادهای اخیر</h2>${ov.incidents.length ? `<div class="list">${ov.incidents.slice(0, 6).map(incRow).join('')}</div>` : '<div class="empty">همه‌چیز آرام است ✨</div>'}</div>
    <div class="card"><h2>💰 هزینه و اعتبار تیم</h2><div id="teamBox"><span class="spin"></span></div></div>
    <div class="card"><h2>⚡ عملیات زنده</h2><div id="opsFeed" class="tl"><div class="muted small">رویدادهای پایشگر و اقدامات اینجا لحظه‌ای نمایش داده می‌شوند.</div></div></div>
  </div>`;
  bindProps(); refreshCounts(); loadTeam();
};
async function loadTeam() {
  const box = $('#teamBox'); if (!box) return;
  try { const t = await api('/api/team');
    if ($('#kpiBal')) $('#kpiBal').textContent = toman(t.balance_toman);
    box.innerHTML = `<div class="facts"><div class="fact"><small>موجودی</small>${toman(t.balance_toman)}</div><div class="fact"><small>نرخ مصرف</small>${toman(t.total_toman_per_hour)}/ساعت</div>
      <div class="fact"><small>ماهانه (برآورد)</small>${toman(t.total_toman_per_month)}</div><div class="fact"><small>کافی برای</small>${t.hours_left ? fa(t.hours_left) + ' ساعت' : '—'}</div></div>` +
      (window.NghViz ? NghViz.render({kind: 'chart', type: 'pie', title: 'سهم هزینه‌ی ساعتی هر برنامه', unit: 'تومان/ساعت', series: t.apps.map(a => ({name: a.project_id, value: Math.round(a.totalHourlyPrice)}))}) : '');
  } catch (e) { box.textContent = e.message; }
}
function appCard(a) {
  const L = a.last || {}, lq = a.liara || {}, pol = a.policy || {};
  const up = L.ok, dotc = L.ts ? (up ? (Object.keys(L.found || {}).length || (L.found || []).length ? 'warn' : 'on') : 'bad') : '';
  const found = Array.isArray(L.found) ? L.found : Object.keys(L.found || {});
  return `<div class="card appc">
    <div class="head"><span class="dot ${dotc}"></span><span class="name ltr">${esc(a.name)}</span>
      <span class="chip ${a.role === 'demo' ? 'info' : a.role === 'self' ? 'brand' : ''}">${esc(ROLE_FA[a.role] || a.role)}</span>
      <span class="right">${pol.write_lock ? '<span class="chip warn" title="هیچ تغییری روی این برنامه اجرا نمی‌شود">🔒 فقط پایش</span>' : pol.autopilot ? '<span class="chip ok">🤖 خودران</span>' : ''}</span></div>
    <div class="row small muted"><span>پلن: <b class="ltr">${esc(lq.plan || '—')}</b> ${esc(PLAN_FA[lq.plan] || '')}</span><span>·</span><span>${toman(lq.toman_h)}/ساعت</span>
      ${lq.scale === 0 ? '<span class="chip bad">خاموش</span>' : ''}</div>
    <div class="meters">
      <div class="meter"><small>CPU</small><b>${pct(L.cpu_util)}</b><div class="${barCls(L.cpu_util || 0)}"><i style="width:${Math.min(100, (L.cpu_util || 0) * 100)}%"></i></div>${spark(a.spark.cpu, '#0f766e')}</div>
      <div class="meter"><small>حافظه</small><b>${pct(L.mem_util)}</b><div class="${barCls(L.mem_util || 0)}"><i style="width:${Math.min(100, (L.mem_util || 0) * 100)}%"></i></div>${spark(a.spark.mem, '#7c3aed')}</div>
      <div class="meter"><small>زمان پاسخ</small><b>${L.latency_ms != null ? fa(L.latency_ms) + 'ms' : '—'}</b><div class="${L.latency_ms > 2500 ? 'bar bad' : L.latency_ms > 1000 ? 'bar warn' : 'bar'}"><i style="width:${Math.min(100, (L.latency_ms || 0) / 30)}%"></i></div>${spark(a.spark.lat, '#1d4ed8', 1000)}</div>
    </div>
    <div class="row small">${L.ts ? `<span class="chip ${up ? 'ok' : 'bad'}">${up ? 'سالم' : 'مشکل'} · HTTP ${fa(L.status)}</span>` : '<span class="chip">در انتظار اولین بررسی</span>'}
      ${found.map(f => `<span class="chip warn">${esc(f)}</span>`).join('')}${a.incidents_open ? `<span class="chip bad">${fa(a.incidents_open)} رخداد باز</span>` : ''}
      <span class="muted right">${ago(L.ts)}</span></div>
    <div class="row"><button class="btn sm" data-act="app" data-arg="${esc(a.name)}">جزئیات</button><button class="btn sm" data-act="diag" data-arg="${esc(a.name)}">🔍 عیب‌یابی</button>
      <a class="btn sm ghost" href="${esc(a.url)}" target="_blank" rel="noopener">باز کردن سایت ↗</a></div>
  </div>`;
}
function incRow(i) {
  const d = i.diagnosis || {}, top = (d.hypotheses || [])[0];
  const ai = d.llm_used ? '<span class="chip info" title="تشخیص قاعده‌محور + توضیح مدل زبانی">⚙️+🧠</span>' : d.hypotheses ? '<span class="chip" title="تشخیص کاملاً قاعده‌محور">⚙️ قاعده‌محور</span>' : '';
  return `<div class="item"><div class="row"><b class="sev-${esc(i.severity)}">${esc(i.title)}</b>${ai}<span class="chip ${i.status === 'resolved' ? 'ok' : i.status === 'mitigating' ? 'info' : 'bad'}">${esc(INC_FA[i.status] || i.status)}</span>
    <span class="right muted small">${esc(i.app)} · ${ago(i.opened_at)}</span></div>
    ${top ? `<div class="small muted">علت محتمل: ${esc(top.title)} (${pct(top.confidence)})</div>` : ''}
    <div class="row"><button class="btn sm" data-act="inc" data-arg="${esc(i.id)}">مشاهده‌ی تحلیل</button></div></div>`;
}

/* ---------------------------------------------------------------- proposals */
function propCard(p, live) {
  const im = p.impact || {}, st = p.status;
  const diff = im.diff ? `<details ${st === 'pending' ? 'open' : ''}><summary>تغییرات کد (diff)</summary><div class="diff">${colorDiff(im.diff)}</div></details>` : '';
  const tests = im.tests_after ? `<div class="row small"><span class="chip ${im.tests_before && im.tests_before.passed ? 'ok' : 'bad'}">تست قبل از وصله: ${esc(im.tests_before ? im.tests_before.summary : '—')}</span><span class="chip ${im.tests_after.passed ? 'ok' : 'bad'}">بعد از وصله: ${esc(im.tests_after.summary)}</span></div>` : '';
  const ver = p.verification ? `<div class="checks">${(p.verification.checks || []).map(c => `<div>${c.ok ? '✅' : '❌'} ${esc(c.name)} — <span class="muted">${esc(c.detail)}</span></div>`).join('')}</div>` : '';
  const res = p.result && p.result.error ? `<div class="err small">${esc(p.result.error)}</div>` : '';
  const before = im.before ? `<div class="small muted">پیش از اقدام: CPU ${pct(im.before.cpu_util)} · حافظه ${pct(im.before.mem_util)} · پلن ${esc(im.before.plan || '')}${im.before.latency_ms ? ' · پاسخ ' + fa(im.before.latency_ms) + 'ms' : ''}</div>` : '';
  const secIn = p.tool === 'launch_project' && st === 'pending' && ((im.secrets_missing || []).length || (im.env_missing || []).length) ? `<div class="form small"><b>مقادیر متغیرهای لازم (مقادیر محرمانه در هیچ گزارشی نمایش داده نمی‌شود)</b>
    ${[...(im.secrets_missing || []).map(n => [n, 1]), ...(im.env_missing || []).map(n => [n, 0])].map(([n, sec]) => `<label class="ltr">${esc(n)}${sec ? ' 🔒' : ''}<input ${sec ? 'type="password"' : ''} data-secin="${esc(p.id)}" data-name="${esc(n)}" autocomplete="off"></label>`).join('')}</div>` : '';
  const steps = p.tool === 'launch_project' && im.steps ? `<ol class="small">${im.steps.map(x => `<li>${esc(x)}</li>`).join('')}</ol>` : '';
  const build = ['launch_project', 'deploy_workspace', 'apply_code_fix', 'deploy_template', 'deploy_project'].includes(p.tool) ? `<div class="logbox small hidden" data-buildfor="${esc(p.id)}" style="max-height:180px"></div>` : '';
  const nxt = p.result && p.result.next_steps ? `<div class="small"><b>گام‌های بعدی:</b><ul>${p.result.next_steps.map(x => `<li>${esc(x)}</li>`).join('')}</ul>${p.result.url ? `<a class="btn sm primary" href="${esc(p.result.url)}" target="_blank" rel="noopener">باز کردن سایت ↗</a>` : ''}</div>` : '';
  const envIn = p.tool === 'set_env' && st === 'pending' && !(p.args || {}).restore ? `<label class="small">مقدار متغیر ${esc((p.args || {}).key)} (محرمانه می‌ماند)<input type="password" data-envval="${esc(p.id)}" autocomplete="off"></label>` : '';
  const delIn = (p.tool === 'delete_app' || p.tool === 'delete_server') && st === 'pending' ? `<label class="small">برای تأیید حذف، نام برنامه را تایپ کنید<input data-delname="${esc(p.id)}" class="ltr"></label>` : '';
  return `<div class="prop ${esc(st)}" data-prop="${esc(p.id)}">
    <div class="row"><span class="t">${esc(p.title)}</span><span class="chip ${st === 'pending' ? 'warn' : st === 'done' ? 'ok' : ['failed', 'rejected', 'stale', 'unverified', 'expired'].includes(st) ? 'bad' : 'info'}">${esc(STATUS_FA[st] || st)}</span>
      ${p.auto ? '<span class="chip ok">🤖 خودران</span>' : ''}${p.decision === 'rules' ? '<span class="chip" title="تصمیم بر اساس قواعد قطعی، بدون مدل زبانی">⚙️ قاعده‌محور</span>' : p.decision === 'ai' ? '<span class="chip info" title="پیشنهاد با کمک هوش مصنوعی">🧠 با کمک AI</span>' : ''}<span class="right muted small">${esc(ORIGIN_FA[p.origin] || p.origin)} · ${ago(p.created_at)}</span></div>
    ${p.why ? `<div>${esc(p.why)}</div>` : ''}
    ${im.blocked ? `<div class="chip bad">${esc(im.blocked)}</div>` : ''}${im.balance_warning ? `<div class="chip warn">${esc(im.balance_warning)}</div>` : ''}
    <div class="facts"><div class="fact"><small>قطعی</small>${esc(im.downtime || '—')}</div><div class="fact"><small>هزینه</small>${esc(im.cost || '—')}</div>
      <div class="fact"><small>ریسک</small>${esc(im.risk || '—')}</div>${im.resources ? `<div class="fact"><small>منابع</small>${esc(im.resources)}</div>` : ''}${im.deploys ? `<div class="fact"><small>سهمیه</small>${esc(im.deploys)}</div>` : ''}</div>
    ${p.rollback ? `<div class="small"><b>برنامه‌ی بازگشت:</b> ${esc(p.rollback)}</div>` : ''}
    ${before}${tests}${steps}${diff}${envIn}${secIn}${delIn}${build}
    ${['executing', 'verifying'].includes(st) ? `<div class="row small"><span class="spin"></span><span data-opfor="${esc(p.id)}">${st === 'verifying' ? 'در حال راستی‌آزمایی…' : 'در حال اجرا…'}</span></div>` : ''}
    ${ver}${res}${nxt}${p.result && p.result.secret_once ? `<button class="btn sm" data-secret="${esc(p.result.secret_once)}">🔑 نمایش یک‌باره‌ی رمز</button><span class="mono small" data-secretout="${esc(p.result.secret_once)}"></span>` : ''}
    ${st === 'pending' ? `<div class="row"><button class="btn primary" data-approve="${esc(p.id)}">✔ تأیید و اجرا</button><button class="btn" data-reject="${esc(p.id)}">رد</button><span class="muted small right">مهلت: ${left(p.expires_at)}</span></div>` : ''}
  </div>`;
}
function colorDiff(d) { return esc(d).split('\n').map(l => l.startsWith('+') && !l.startsWith('+++') ? `<span class="a">${l}</span>` : l.startsWith('-') && !l.startsWith('---') ? `<span class="d">${l}</span>` : l.startsWith('@@') ? `<span class="h">${l}</span>` : l).join('\n'); }
function bindProps() {
  $$('[data-approve]').forEach(b => b.onclick = async () => {
    const id = b.dataset.approve, body = {}; const ev = $(`[data-envval="${id}"]`), dn = $(`[data-delname="${id}"]`);
    if (ev) { if (!ev.value) { toast('مقدار متغیر را وارد کنید'); return; } body.value = ev.value; }
    if (dn) body.confirm_name = dn.value;
    const secs = $$(`[data-secin="${id}"]`); if (secs.length) { body.envs = {}; secs.forEach(i => { if (i.value) body.envs[i.dataset.name] = i.value; }); }
    b.disabled = true; b.textContent = 'در حال ارسال…';
    try { const p = await api(`/api/proposals/${id}/approve`, {method: 'POST', body}); toast('تأیید شد؛ در حال اجرا…'); const el = $(`[data-prop="${id}"]`); if (el) { el.outerHTML = propCard(p); bindProps(); } }
    catch (e) { toast(e.message, 6000); b.disabled = false; b.textContent = '✔ تأیید و اجرا'; }
  });
  $$('[data-secret]').forEach(b => b.onclick = async () => { b.disabled = true; try { const r = await api(b.dataset.secret.startsWith('/') ? b.dataset.secret : '/api/diag/servers/secret/' + b.dataset.secret, {method: 'POST'}); const o = $(`[data-secretout="${b.dataset.secret}"]`); if (o) o.textContent = r.value || JSON.stringify(r); b.remove(); } catch (e) { toast(e.message); b.disabled = false; } });
  $$('[data-reject]').forEach(b => b.onclick = async () => { try { const p = await api(`/api/proposals/${b.dataset.reject}/reject`, {method: 'POST'}); const el = $(`[data-prop="${p.id}"]`); if (el) el.outerHTML = propCard(p); toast('رد شد'); } catch (e) { toast(e.message); } });
}
VIEWS.approvals = async () => {
  const v = $('#view'); const ps = await api('/api/proposals');
  const pend = ps.filter(p => p.status === 'pending'), rest = ps.filter(p => p.status !== 'pending');
  v.innerHTML = `<div class="card small muted" style="margin-bottom:12px">هر کار حساس (ری‌استارت، تغییر پلن، تغییر متغیر، استقرار، بازگشت نسخه، حذف) فقط با تأیید شما اجرا می‌شود. هر تأیید یک‌بارمصرف است، ۲۰ دقیقه اعتبار دارد و اگر وضعیت برنامه در این فاصله عوض شود باطل می‌شود. پس از اجرا، نگهبان نتیجه را خودش راستی‌آزمایی می‌کند.</div>
  <div class="sec-title"><h2>منتظر تأیید (${fa(pend.length)})</h2></div><div class="list">${pend.map(p => propCard(p)).join('') || '<div class="empty card">کاری منتظر تأیید نیست</div>'}</div>
  <div class="sec-title"><h2>سابقه</h2></div><div class="list">${rest.slice(0, 40).map(p => propCard(p)).join('')}</div>`;
  bindProps();
};

/* ---------------------------------------------------------------- incidents & diagnosis */
VIEWS.incidents = async () => {
  const v = $('#view'); const inc = await api('/api/incidents');
  v.innerHTML = `<div class="list">${inc.map(incRow).join('') || '<div class="empty card">رخدادی ثبت نشده است</div>'}</div>`;
};
window.openIncident = async id => {
  const i = await api('/api/incidents/' + id);
  modal(`<h2>${esc(i.title)}</h2><div class="row small"><span class="chip">${esc(i.app)}</span><span class="chip ${i.status === 'resolved' ? 'ok' : 'bad'}">${esc(INC_FA[i.status])}</span><span class="muted">${dt(i.opened_at)}</span>
    ${i.status !== 'resolved' ? `<button class="btn sm right" data-act="resolve" data-arg="${esc(i.id)}">بستن رخداد</button>` : ''}</div>
    ${diagHtml(i.diagnosis || {}, i.id)}
    <h3>روند رخداد</h3><div class="tl">${(i.timeline || []).map(t => `<div><time>${clock(t.ts)}</time>${esc(t.text)}</div>`).join('')}</div>
    ${(i.proposals || []).length ? `<h3>اقدامات</h3><div class="list">${i.proposals.map(p => propCard(p)).join('')}</div>` : ''}`);
  bindDiag(); bindProps();
};
window.resolveInc = async id => { await api(`/api/incidents/${id}/resolve`, {method: 'POST'}); $('#modal').classList.add('hidden'); toast('رخداد بسته شد'); };
function diagHtml(d, incId) {
  if (!d || !d.hypotheses) return d && d.trigger ? `<p class="muted">علت شروع: ${esc(d.trigger)} — تحلیل در حال انجام است…</p>` : '';
  const ev = d.evidence || {}, L = ev.logs || {};
  const H = d.hypotheses.map((h, k) => `<div class="hyp"><div class="row"><b>${fa(k + 1)}. ${esc(h.title)}</b>${h.source === 'llm' ? '<span class="chip info">پیشنهاد مدل</span>' : ''}
      <span class="conf right">اطمینان ${pct(h.confidence)}<span class="bar"><i style="width:${h.confidence * 100}%"></i></span></span></div>
      ${h.explanation ? `<div class="small">${esc(h.explanation)}</div>` : ''}
      <ul class="small">${(h.evidence || []).map(e => `<li>${esc(e)}</li>`).join('')}</ul>
      ${(h.fixes || []).length && incId ? `<div class="row">${h.fixes.map((f, j) => `<button class="btn sm ${j ? '' : 'primary'}" data-fix="${esc(incId)}|${esc(h.id)}|${j}">${esc(f.label)}</button>`).join('')}</div>` : (h.fixes || []).length ? `<div class="row small muted">راه‌حل: ${h.fixes.map(f => esc(f.label)).join('، ')}</div>` : ''}</div>`).join('');
  return `${d.summary ? `<div class="card" style="background:var(--brand-soft);border-color:#bfe1dc;margin:10px 0"><b>خلاصه:</b> ${esc(d.summary)}</div>` : ''}
    ${d.healthy ? '<div class="chip ok">مشکلی پیدا نشد؛ برنامه سالم است ✅</div>' : ''}
    <h3>علت‌های محتمل (رتبه‌بندی‌شده)</h3><div class="list">${H || '<div class="muted">فرضیه‌ای ساخته نشد</div>'}</div>
    ${(d.next_steps || []).length ? `<h3>گام‌های بعدی</h3><ul>${d.next_steps.map(s => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}
    <details><summary>شواهد خام (لاگ، متریک، ریلیزها)</summary>
      <div class="facts" style="margin:8px 0"><div class="fact"><small>پاسخ سایت</small>HTTP ${fa((ev.probe || {}).status)} · ${fa((ev.probe || {}).latency_ms)}ms</div>
      <div class="fact"><small>CPU</small>${pct((ev.cpu || {}).util_last)} از سقف</div><div class="fact"><small>حافظه</small>${pct((ev.mem || {}).util_last)} از سقف</div>
      <div class="fact"><small>نرخ خطا</small>${pct(L.error_rate)} (${fa(L.errors_5xx)} پاسخ 5xx)</div></div>
      ${(L.tracebacks || []).map(t => `<div class="logbox">${esc(t.sample)}</div>`).join('')}
      <div class="logbox" style="margin-top:8px">${(ev.log_tail || []).map(l => `<div class="${/error|traceback|exception/i.test(l) ? 'e' : /warn|slow/i.test(l) ? 'w' : ''}">${esc(l)}</div>`).join('')}</div>
    </details>`;
}
function bindDiag() {
  $$('[data-fix]').forEach(b => b.onclick = async () => {
    const [inc, hyp, fix] = b.dataset.fix.split('|'); b.disabled = true; b.innerHTML = '<span class="spin"></span> در حال آماده‌سازی…';
    try { const r = await api(`/api/incidents/${inc}/act`, {method: 'POST', body: {hypothesis: hyp, fix: +fix}});
      if (r.proposal) { toast('کارت تأیید ساخته شد'); openIncident(inc); }
      else if (r.status === 'proposed') { toast('وصله آماده و تست شد؛ کارت تأیید ساخته شد'); openIncident(inc); }
      else toast(r.message || r.error || r.status || 'انجام شد', 7000);
    } catch (e) { toast(e.message, 6000); } finally { b.disabled = false; }
  });
}
window.runDiag = async (app, btn) => {
  const t = btn ? btn.innerHTML : ''; if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> در حال عیب‌یابی…'; }
  try { const d = await api(`/api/apps/${app}/diagnose`, {method: 'POST'}); modal(`<h2>عیب‌یابی ${esc(app)}</h2>${diagHtml(d, null)}<div class="muted small">برای اجرای راه‌حل از «رخدادها» یا گفتگو استفاده کنید؛ نگهبان برای هر اقدام کارت تأیید می‌سازد.</div>`); }
  catch (e) { toast(e.message); } finally { if (btn) { btn.disabled = false; btn.innerHTML = t; } }
};

/* ---------------------------------------------------------------- app detail */
VIEWS.app = async name => {
  const v = $('#view'); v.innerHTML = '<div class="empty"><span class="spin"></span> در حال بارگذاری همه‌ی اطلاعات برنامه…</div>';
  const hours = +(S.appHours || 6);
  const [d, f] = await Promise.all([api('/api/apps/' + name), api(`/api/apps/${name}/full?hours=${hours}`).catch(() => null)]);
  const l = d.liara, cur = d.current_release, run = (f && f.running) || {};
  const ch = (title, series, unit, limit, lab) => `<div class="card"><h3>${title}</h3>${window.NghViz ? NghViz.timeChart({type: 'area', unit, limit, limit_label: lab, series}) : ''}</div>`;
  const disks = (run.disks || []).map(k => `<div class="fact"><small>دیسک ${esc(k.name)}</small>${k.usage != null ? fa(Math.round(k.usage)) + '٪ پر' : '—'} از ${k.size ? fa(Math.round(k.size / 1024 ** 3)) + ' GB' : '؟'}</div>`).join('');
  const EVFA = {oom: '💥 OOM (کمبود حافظه)', die: '⛔ توقف', kill: '🛑 kill', start: '▶️ شروع', create: '🆕 ساخت', restart: '🔁 ری‌استارت', stop: '⏹ توقف', update: 'به‌روزرسانی', rename: 'تغییر نام', destroy: '🗑 حذف', destory: '🗑 حذف'};
  v.innerHTML = `<div class="row" style="margin-bottom:12px"><button class="btn sm" data-act="go" data-arg="overview">→ بازگشت</button><h2 class="ltr" style="margin:0">${esc(name)}</h2>
     <span class="chip">${esc(l.planID)} ${esc(PLAN_FA[l.planID] || '')}</span><span class="chip">باندل ${esc(l.bundlePlanID)}</span><span class="chip ${l.scale ? 'ok' : 'bad'}">${l.scale ? 'روشن' : 'خاموش'}</span>
     <span class="chip">${toman((l.hourlyPrice || 0) + (l.bundleHourlyPrice || 0))}/ساعت</span><span class="chip ${l.zeroDowntime ? 'ok' : ''}">استقرار بدون قطعی: ${l.zeroDowntime ? 'روشن' : 'خاموش'}</span>
     <span class="right row"><select id="hrs" style="width:auto">${[1, 6, 24, 72, 168].map(h => `<option value="${h}" ${h === hours ? 'selected' : ''}>${fa(h)} ساعت</option>`).join('')}</select>
     <button class="btn sm primary" data-act="diag" data-arg="${esc(name)}">🔍 عیب‌یابی کامل</button></span></div>
  <div class="card" style="margin-bottom:16px"><h3>آنچه در حال اجراست</h3><div class="facts">
     <div class="fact"><small>پلتفرم / پورت</small>${esc(run.platform || '—')} / ${fa(run.port)}</div><div class="fact"><small>نسخه‌ی فعلی</small>${esc(run.release_tag || '—')} <span class="muted small">${esc((run.release_message || '').slice(0, 40))}</span></div>
     <div class="fact"><small>ایمیج</small><span class="mono">${esc(run.image || '—')}</span></div><div class="fact"><small>مدت روشن بودن</small>${run.uptime_minutes != null ? fa(run.uptime_minutes) + ' دقیقه' : '—'}</div>
     <div class="fact"><small>محل بیلد</small>${esc(run.build_location || '—')}</div><div class="fact"><small>دامنه‌ها</small>${(run.domains || []).map(x => `<a class="ltr" href="https://${esc(x)}" target="_blank" rel="noopener">${esc(x)}</a>`).join('<br>')}</div>${disks}</div></div>
  <div class="grid g2">
    ${f ? ch('CPU (هسته)', [{name: 'CPU', points: f.cpu}], 'هسته', f.plan.cpu, 'سقف پلن') : ''}
    ${f ? ch('حافظه (گیگابایت)', [{name: 'RAM', points: f.mem}], 'GB', f.plan.ram, 'سقف پلن') : ''}
    ${f ? ch('شبکه (KB/s)', [{name: 'دریافت', points: f.rx}, {name: 'ارسال', points: f.tx}], 'KB/s') : ''}
    <div class="card"><h3>رویدادهای کانتینر (۳ روز)</h3><div class="tl" style="max-height:230px;overflow:auto">${f && f.events.length ? f.events.filter(x => !String(x.action || '').startsWith('exec_')).map(x => `<div><time>${dt(x.time)}</time>${esc(EVFA[x.action] || String(x.action || '').slice(0, 40))} <span class="muted small">${esc(x.tag || '')}</span></div>`).join('') : '<div class="muted small">رویدادی نیست</div>'}</div></div>
  </div>
  <div class="card" style="margin-top:16px"><div class="row"><h3 style="margin:0">لاگ‌ها</h3><select id="logMin" style="width:auto"><option value="15">۱۵ دقیقه</option><option value="60">۱ ساعت</option><option value="360">۶ ساعت</option></select>
    <input id="logQ" placeholder="جستجو (حداقل ۳ حرف)" style="max-width:220px"><label class="switch small"><input type="checkbox" id="logErr"> فقط خطاها</label><button class="btn sm" id="logGo">نمایش</button></div><div id="logs" class="logbox" style="margin-top:10px">…</div></div>
  <div class="grid g2" style="margin-top:16px">
    <div class="card"><h3>ریلیزها</h3><div class="tablewrap"><table><tr><th>نسخه</th><th>وضعیت</th><th>نوع</th><th>پیام</th><th>زمان</th><th></th></tr>
      ${d.releases.map(r => `<tr><td>${esc(r.tag)}${r._id === cur ? ' <span class="chip ok">فعلی</span>' : ''}</td><td><span class="chip ${r.state === 'READY' ? 'ok' : ['FAILED', 'UNHEALTHY', 'TIMEDOUT'].includes(r.state) ? 'bad' : 'info'}">${esc(r.state)}</span>${r.failReason ? `<div class="small err">${esc(r.failReason)}</div>` : ''}</td><td>${esc(r.type)}</td><td class="small">${esc((r.message || '').slice(0, 60))}</td><td class="small">${esc(new Date(r.createdAt).toLocaleString('fa-IR'))}</td><td>${r.type === 'DEPLOY' ? `<button class="btn sm" data-bl="${esc(r._id)}" data-tag="${esc(r.tag)}">لاگ بیلد</button>` : ''}</td></tr>`).join('')}</table></div></div>
    <div class="card"><h3>متغیرهای محیطی <span class="muted small">(فقط نام‌ها؛ مقادیر حساس پنهان است)</span></h3><table>${d.envs.map(e => `<tr><td class="mono">${esc(e.key)}</td><td class="mono">${esc(e.value)}</td></tr>`).join('') || '<tr><td class="muted">—</td></tr>'}</table></div>
  </div>
  ${d.commits.length ? `<div class="card" style="margin-top:16px"><h3>تاریخچه‌ی کد (فضای کاری نگهبان)</h3><div class="tl">${d.commits.map(c => `<div><time>${dt(+c.ts)}</time><code>${esc(c.sha)}</code> ${esc(c.message)} <span class="muted small">— ${esc(c.author)}</span></div>`).join('')}</div></div>` : ''}
  <div class="grid g2" style="margin-top:16px">
    <div class="card"><h3>رخدادهای این برنامه</h3>${d.incidents.length ? `<div class="list">${d.incidents.slice(0, 6).map(incRow).join('')}</div>` : '<div class="muted small">رخدادی ثبت نشده</div>'}</div>
    <div class="card"><h3>آرشیو سورس‌ها (برای بازگشت)</h3>${d.archives.length ? `<table>${d.archives.map(a => `<tr><td class="small">${dt(a.ts)}</td><td class="small">${esc(a.message || '')}</td><td><span class="chip">${esc(a.state || '')}</span></td><td class="small mono">${esc((a.sha256 || '').slice(0, 10))}</td></tr>`).join('')}</table>` : '<div class="muted small">هنوز استقراری از طریق نگهبان انجام نشده</div>'}</div>
  </div>
  <div class="card" style="margin-top:16px"><h3>گزارش عملیات این برنامه</h3><div class="tl">${d.audit.slice(0, 25).map(a => `<div><time>${dt(a.ts)}</time>${a.ok ? '' : '❌ '}<b>${esc(ACT_FA[a.action] || a.action)}</b> <span class="muted small">(${esc(a.actor)})</span></div>`).join('') || '<div class="muted small">—</div>'}</div></div>`;
  $('#hrs').onchange = () => { S.appHours = +$('#hrs').value; VIEWS.app(name); };
  $$('[data-bl]').forEach(b => b.onclick = async () => {
    const t = b.textContent; b.disabled = true; b.innerHTML = '<span class="spin"></span>';
    try { const r = await api(`/api/releases/${b.dataset.bl}/build-logs`); modal(`<h2>لاگ بیلد ${esc(b.dataset.tag)}</h2><div class="row"><span class="chip ${r.release.state === 'READY' ? 'ok' : 'bad'}">${esc(r.release.state)}</span>${r.release.failReason ? `<span class="chip bad">${esc(r.release.failReason)}</span>` : ''}${r.summary.missing_package ? `<span class="chip bad">بسته‌ی ناموجود: ${esc(r.summary.missing_package)}</span>` : ''}</div>
      <div class="logbox" style="margin-top:10px;max-height:60vh">${r.lines.map(x => `<div class="${/error|failed/i.test(x) ? 'e' : ''}">${esc(x)}</div>`).join('')}</div>`); }
    catch (e) { toast(e.message); } finally { b.disabled = false; b.textContent = t; }
  });
  const load = async () => { const box = $('#logs'); box.innerHTML = '<span class="spin"></span>'; $('#logGo').disabled = true;
    try { let ls = await api(`/api/apps/${name}/logs?minutes=${$('#logMin').value}&search=${encodeURIComponent($('#logQ').value)}`);
      if ($('#logErr').checked) ls = ls.filter(l => /error|exception|traceback|critical|fatal|" 5\d\d |failed|killed|  File "/i.test(l.line));
      box.innerHTML = ls.map(l => `<div class="${/error|traceback|exception|critical/i.test(l.line) ? 'e' : /warn|slow/i.test(l.line) ? 'w' : ''}">${clock(l.ts)} ${esc(l.line)}</div>`).join('') || 'لاگی در این بازه نیست'; box.scrollTop = box.scrollHeight; }
    catch (e) { box.textContent = e.message; } finally { $('#logGo').disabled = false; } };
  $('#logGo').onclick = load; $('#logErr').onchange = load; load();
};

/* ---------------------------------------------------------------- chat */
const SUGGEST = ['چه برنامه‌هایی در حال اجراست؟', 'نمودار حافظه‌ی bazargard-ai در ۶ ساعت اخیر را نشان بده', 'هزینه‌ها را به تفکیک برنامه با نمودار نشان بده', 'نقشه‌ی ارتباط برنامه‌ها را بکش', 'چرا demo-shop خطای ۵۰۰ می‌دهد؟', '۵۰ خط آخر خطاهای meowmeow-wallex را نشان بده', 'demo-shop را ری‌استارت کن'];
VIEWS.chat = async () => {
  const v = $('#view');
  v.innerHTML = `<div class="chat"><div class="msgs" id="msgs"></div><div><div class="suggest" id="sugg">${SUGGEST.map(s => `<button class="btn sm">${esc(s)}</button>`).join('')}</div>
    <form class="composer" id="composer"><textarea id="ask" placeholder="از نگهبان بپرسید… مثلاً «سایتم کند شده»" maxlength="3000"></textarea><button class="btn primary" id="send">ارسال</button></form></div></div>`;
  const hist = await api('/api/chat/' + S.session).catch(() => []);
  for (const m of hist) addMsg(m.role === 'system_note' ? 'note' : m.role, m.content, m.meta);
  if (!hist.length) addMsg('assistant', 'سلام! من نگهبان هستم 🛡️ همکار فنی شما برای سایت‌های روی لیارا. وضعیت، لاگ، مصرف منابع و هزینه را بررسی می‌کنم، علت خطاها را پیدا می‌کنم و برای هر تغییر حساس اول از شما اجازه می‌گیرم. چه کمکی از دستم برمی‌آید؟');
  $$('#sugg button').forEach(b => b.onclick = () => { $('#ask').value = b.textContent; send(); });
  $('#composer').onsubmit = e => { e.preventDefault(); send(); };
  $('#ask').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } };
  $('#sugg').classList.toggle('hidden', hist.length > 2);
};
function addMsg(role, text, meta) {
  const box = $('#msgs'); if (!box) return;
  const div = document.createElement('div'); div.className = 'msg ' + (role === 'user' ? 'user' : role === 'note' ? 'note' : 'assistant');
  div.innerHTML = md(text);
  if (meta && meta.steps && meta.steps.length) { const s = document.createElement('div'); s.className = 'steps'; s.innerHTML = meta.steps.map(st => `<div>${st.ok ? '✓' : '✗'} ${esc(st.label.replace('در حال ', '').replace('…', ''))}</div>`).join(''); box.appendChild(s); }
  if (meta && meta.attachments) meta.attachments.forEach(a => addAttach(a, box));
  box.appendChild(div);
  if (meta && meta.steps) meta.steps.filter(s => s.proposal).forEach(s => loadPropInto(box, s.proposal));
  box.scrollTop = box.scrollHeight;
}
function md(text) {  /* tiny, safe markdown: escape first, then headings, bold, code, bullets */
  return esc(text).split('\n').map(l => {
    const h = l.match(/^#{1,4}\s+(.*)$/); if (h) return `<b class="mdh">${h[1]}</b>`;
    return l.replace(/^\s*[-*]\s+/, '• ');
  }).join('\n').replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>');
}
function addAttach(att, box) {
  box = box || $('#msgs'); if (!box) return;
  const w = document.createElement('div'); w.className = 'msg assistant wide'; w.innerHTML = window.NghViz ? NghViz.render(att) : ''; box.appendChild(w); box.scrollTop = box.scrollHeight;
}
async function loadPropInto(box, id) {
  try { const ps = await api('/api/proposals'); const p = ps.find(x => x.id === id); if (p) { const w = document.createElement('div'); w.style.alignSelf = 'stretch'; w.innerHTML = propCard(p); box.appendChild(w); bindProps(); box.scrollTop = box.scrollHeight; } } catch { }
}
async function send() {
  const t = $('#ask').value.trim(); if (!t || S.busy) return;
  $('#ask').value = ''; addMsg('user', t); $('#sugg').classList.add('hidden'); S.busy = true; $('#send').disabled = true;
  const box = $('#msgs'); const st = document.createElement('div'); st.className = 'steps'; st.id = 'liveSteps'; st.innerHTML = '<div data-th="1"><span class="spin"></span>در حال فکر کردن…</div>'; box.appendChild(st); box.scrollTop = box.scrollHeight;
  const rid = S.reqId = (S.reqId || 0) + 1;
  try { await api('/api/chat', {method: 'POST', body: {session: S.session, message: t}}); pollChat(rid); }
  catch (e) { st.remove(); addMsg('note', e.message); S.busy = false; $('#send').disabled = false; }
}
async function pollChat(rid) {  /* safety net when the live stream is cut by a proxy: fetch the answer from history */
  const sent = Date.now() / 1000;
  for (let i = 0; i < 100 && S.busy && S.reqId === rid; i++) {
    await new Promise(r => setTimeout(r, 3000));
    if (!S.busy || S.reqId !== rid) return;
    try { const h = await api('/api/chat/' + S.session); const last = h[h.length - 1];
      if (last && last.role === 'assistant' && last.ts > sent - 2 && S.busy && S.reqId === rid) { chatEvent({kind: 'chat_done', text: last.content}); (last.meta && last.meta.attachments || []).forEach(a => addAttach(a)); return; } } catch { }
  }
}
function chatEvent(ev) {
  const box = $('#msgs');
  if (ev.kind === 'chat_step' && box) {
    let st = $('#liveSteps'); if (!st) { st = document.createElement('div'); st.className = 'steps'; st.id = 'liveSteps'; box.appendChild(st); }
    if (ev.state === 'thinking') { const last = st.lastElementChild; if (last && last.dataset.th) last.remove(); st.insertAdjacentHTML('beforeend', `<div data-th="1"><span class="spin"></span>${esc(ev.text)}</div>`); }
    else if (ev.state === 'running') { const last = st.lastElementChild; if (last && last.dataset.th) last.remove(); st.insertAdjacentHTML('beforeend', `<div data-tool="${esc(ev.tool)}"><span class="spin"></span>${esc(ev.text)}${ev.risk === 'sensitive' ? ' 🔐' : ''}</div>`); }
    else { const el = [...st.querySelectorAll(`[data-tool="${ev.tool}"]`)].pop(); if (el) el.innerHTML = (ev.state === 'done' ? '✓ ' : '✗ ') + esc(ev.text); if (ev.proposal) loadPropInto(box, ev.proposal); }
    box.scrollTop = box.scrollHeight;
  } else if (ev.kind === 'chat_done') {
    if (!S.busy) return;  // already finalised (stream + polling safety net)
    const st = $('#liveSteps'); if (st) { st.removeAttribute('id'); st.querySelectorAll('[data-th]').forEach(x => x.remove()); }
    addMsg('assistant', ev.text); S.busy = false; if ($('#send')) $('#send').disabled = false;
  } else if (ev.kind === 'chat_note') { addMsg('note', ev.text); }
  else if (ev.kind === 'chat_attach') { addAttach(ev.attachment); }
}

/* ---------------------------------------------------------------- autopilot */
VIEWS.autopilot = async () => {
  const v = $('#view'); const [pol, pl, li] = await Promise.all([api('/api/policies'), api('/api/plans'), api('/api/llm')]);
  const opts = (sel) => pl.order.filter(p => pl.plans[p]).map(p => `<option value="${p}" ${p === sel ? 'selected' : ''}>${p} — ${PLAN_FA[p] || ''} (${fa(pl.plans[p].cpu)} هسته، ${fa(pl.plans[p].ram)} گیگ)</option>`).join('');
  v.innerHTML = `<div class="card small muted" style="margin-bottom:14px">اینجا مشخص می‌کنید نگهبان <b>بدون پرسیدن</b> چه کارهایی انجام دهد. هر کاری بیرون از این سیاست‌ها به‌صورت کارت تأیید از شما پرسیده می‌شود. همه‌ی اقدامات خودکار در «گزارش عملیات» با وضعیت قبل/بعد ثبت می‌شود. لیارا مقیاس افقی ندارد؛ «مقیاس خودکار» یعنی تغییر پلن در بازه‌ای که شما تعیین می‌کنید.</div>
  <div class="card form" style="margin-bottom:16px"><h3>🧠 مدل زبانی</h3>
    <div class="row small"><span class="chip info">فعال: ${esc(li.provider === 'anthropic' ? 'Anthropic' : 'BoxAPI')} · <span class="ltr">${esc(li.model)}</span></span>
      ${li.fallback ? `<span class="chip">پشتیبان خودکار: ${esc(li.fallback === 'anthropic' ? 'Anthropic' : 'BoxAPI')}</span>` : ''}<span class="chip">کلیدهای BoxAPI: ${fa(li.boxapi_keys)}</span>
      <span class="chip">جابه‌جایی خودکار تاکنون: ${fa(li.stats.fallbacks)}</span></div>
    <div class="row"><label class="switch"><input type="radio" name="prov" value="anthropic" ${li.provider === 'anthropic' ? 'checked' : ''} ${li.anthropic_configured ? '' : 'disabled'}> Anthropic (claude-haiku-5-5 از طریق دروازه‌ی تیم)</label>
      <label class="switch"><input type="radio" name="prov" value="boxapi" ${li.provider === 'boxapi' ? 'checked' : ''}> BoxAPI (gpt-6-luna، ۳ کلید چرخشی)</label><button class="btn sm primary" id="provSave">ذخیره</button></div>
    <div class="muted small">اگر ارائه‌دهنده‌ی فعال در دسترس نباشد (۴۲۹، ۵xx، اتمام اعتبار، قطع اضطراری دروازه) درخواست خودکار به دیگری می‌رود.</div></div>
  <div class="grid g2">${Object.entries(pol).filter(([n, p]) => p.role !== 'off').map(([name, p]) => `<form class="card form" data-pol="${esc(name)}">
    <div class="row"><h3 class="ltr" style="margin:0">${esc(name)}</h3><span class="chip">${esc(ROLE_FA[p.role] || p.role)}</span></div>
    ${p.role === 'self' ? '<div class="chip brand">پلن خود نگهبان ثابت است و فقط توسط نگهبانِ بیرونی (watchdog) ری‌استارت می‌شود.</div>' : ''}
    <label class="switch"><input type="checkbox" name="write_lock" ${p.write_lock ? 'checked' : ''} ${p.role === 'self' ? 'disabled' : ''}> 🔒 فقط پایش (هیچ تغییری اجرا نشود، حتی با تأیید)</label>
    <label class="switch"><input type="checkbox" name="env_lock" ${p.env_lock ? 'checked' : ''}> قفل متغیرهای محیطی</label>
    <label class="switch"><input type="checkbox" name="autopilot" ${p.autopilot ? 'checked' : ''}> 🤖 خودران فعال باشد</label>
    <fieldset class="form" style="border:1px solid var(--line);border-radius:12px;padding:10px"><legend class="small">مقیاس خودکار (تغییر پلن)</legend>
      <label class="switch"><input type="checkbox" name="resize" ${p.resize ? 'checked' : ''}> ارتقای خودکار پلن هنگام فشار منابع</label>
      <label class="switch"><input type="checkbox" name="scale_down" ${p.scale_down ? 'checked' : ''}> کاهش خودکار پلن پس از مصرف کم</label>
      <div class="row"><label class="grow">حداقل پلن<select name="min_plan">${opts(p.min_plan)}</select></label><label class="grow">حداکثر پلن<select name="max_plan">${opts(p.max_plan)}</select></label></div>
      <div class="row"><label class="grow">سقف هزینه‌ی روزانه (تومان)<input name="max_toman_day" type="number" min="0" step="1000" value="${p.max_toman_day}"></label></div>
      <div class="row"><label class="grow">ارتقا اگر CPU ≥ (٪)<input name="up_cpu" type="number" min="30" max="100" value="${Math.round(p.up_cpu * 100)}"></label><label class="grow">یا حافظه ≥ (٪)<input name="up_mem" type="number" min="30" max="100" value="${Math.round(p.up_mem * 100)}"></label><label class="grow">به مدت (دقیقه)<input name="up_minutes" type="number" min="2" max="60" value="${p.up_minutes}"></label></div>
      <div class="row"><label class="grow">کاهش اگر CPU < (٪)<input name="down_cpu" type="number" min="5" max="80" value="${Math.round(p.down_cpu * 100)}"></label><label class="grow">و حافظه < (٪)<input name="down_mem" type="number" min="5" max="80" value="${Math.round(p.down_mem * 100)}"></label><label class="grow">به مدت (دقیقه)<input name="down_minutes" type="number" min="5" max="240" value="${p.down_minutes}"></label></div>
    </fieldset>
    <label class="switch"><input type="checkbox" name="restart_on_crash" ${p.restart_on_crash ? 'checked' : ''}> ری‌استارت خودکار در کرش یا نشت حافظه</label>
    <label class="switch"><input type="checkbox" name="auto_rollback" ${p.auto_rollback ? 'checked' : ''}> بازگشت خودکار به نسخه‌ی قبلی وقتی نسخه‌ی جدید خطا می‌دهد</label>
    ${p.role === 'demo' ? `<label class="switch"><input type="checkbox" name="auto_fix_code" ${p.auto_fix_code ? 'checked' : ''}> اعمال خودکار اصلاح کد (فقط اگر همه‌ی تست‌ها پاس شوند)</label>` : ''}
    <button class="btn primary" type="submit">ذخیره</button></form>`).join('')}</div>`;
  $('#provSave').onclick = async () => { const b = $('#provSave'); b.disabled = true; try { const p = ($('input[name=prov]:checked') || {}).value; const r = await api('/api/llm', {method: 'PUT', body: {provider: p}}); toast('ارائه‌دهنده: ' + r.provider + ' · ' + r.model); } catch (e) { toast(e.message); } finally { b.disabled = false; } };
  $$('[data-pol]').forEach(f => f.onsubmit = async e => {
    e.preventDefault(); const fd = new FormData(f), body = {};
    for (const k of ['write_lock', 'env_lock', 'autopilot', 'resize', 'scale_down', 'restart_on_crash', 'auto_rollback', 'auto_fix_code']) if (f.elements[k]) body[k] = f.elements[k].checked;
    for (const k of ['min_plan', 'max_plan']) body[k] = fd.get(k);
    body.max_toman_day = +fd.get('max_toman_day'); for (const k of ['up_cpu', 'up_mem', 'down_cpu', 'down_mem']) body[k] = (+fd.get(k)) / 100;
    for (const k of ['up_minutes', 'down_minutes']) body[k] = +fd.get(k);
    try { await api('/api/policies/' + f.dataset.pol, {method: 'PUT', body}); toast('سیاست ذخیره شد ✓'); } catch (err) { toast(err.message); }
  });
};

/* ---------------------------------------------------------------- new project wizard */
VIEWS.wizard = () => {
  const v = $('#view');
  v.innerHTML = `<div class="steps-wiz"><span class="on">۱. تحلیل پروژه</span><span>۲. پیشنهاد پلن و قیمت</span><span>۳. ساخت برنامه (تأیید)</span><span>۴. استقرار (تأیید)</span><span>۵. بررسی سلامت</span></div>
  <div class="grid g2"><div class="card form"><h3>سورس پروژه</h3>
    <label>فایل zip پروژه<input type="file" id="zip" accept=".zip"></label><div class="muted small">یا</div>
    <label>آدرس مخزن عمومی git (https)<input id="git" class="ltr" placeholder="https://github.com/user/repo"></label>
    <button class="btn primary" id="anl">🔍 تحلیل پروژه</button><div class="muted small">فایل‌ها فقط خوانده می‌شوند و هیچ کدی از آن‌ها اجرا نمی‌شود.</div></div>
    <div class="card" id="wizOut"><div class="empty">نتیجه‌ی تحلیل اینجا نمایش داده می‌شود</div></div></div>`;
  $('#anl').onclick = async () => {
    const out = $('#wizOut'); out.innerHTML = '<div class="row"><span class="spin"></span> در حال تحلیل…</div>';
    try {
      let a; if ($('#zip').files[0]) { const fd = new FormData(); fd.append('file', $('#zip').files[0]); a = await api('/api/projects/analyze', {method: 'POST', body: fd}); }
      else a = await api('/api/projects/analyze-git', {method: 'POST', body: {git_url: $('#git').value.trim()}});
      wizResult(a);
    } catch (e) { out.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  };
};
function wizResult(a) {
  $$('.steps-wiz span')[1].classList.add('on');
  const rows = Object.entries(a.pricing).map(([p, x]) => `<tr ${p === a.recommended_plan ? 'style="background:var(--brand-soft)"' : ''}><td><label class="switch"><input type="radio" name="plan" value="${p}" ${p === a.recommended_plan ? 'checked' : ''}> ${p}</label></td><td>${esc(x.name_fa)}</td><td>${fa(x.cpu)} هسته / ${fa(x.ram)} گیگ</td><td>${toman(x.hour)}</td><td>${toman(x.month)}</td></tr>`).join('');
  $('#wizOut').innerHTML = `<h3>نتیجه‌ی تحلیل</h3><div class="facts"><div class="fact"><small>نوع پروژه</small>${esc(a.framework || '؟')}</div><div class="fact"><small>پلتفرم لیارا</small>${esc(a.platform)}</div>
    <div class="fact"><small>پورت</small>${fa(a.port)}</div><div class="fact"><small>فایل‌ها</small>${fa(a.files)} (${fa(a.size_mb)} MB)</div></div>
    ${a.env_vars.length ? `<p class="small">متغیرهای محیطی لازم: ${a.env_vars.map(e => `<code>${esc(e)}</code>`).join(' ')}</p>` : ''}${a.databases.length ? `<p class="small">دیتابیس: ${esc(a.databases.join('، '))}</p>` : ''}
    ${a.notes.map(n => `<div class="chip warn" style="margin:2px">${esc(n)}</div>`).join('')}
    <p><b>${esc(a.recommendation_fa)}</b></p>
    <div class="tablewrap"><table><tr><th>پلن</th><th>نام</th><th>منابع</th><th>ساعتی</th><th>ماهانه (با باندل برنزی)</th></tr>${rows}</table></div>
    <div class="form" style="margin-top:10px"><label>نام برنامه در لیارا (انگلیسی، کوچک)<input id="appName" class="ltr" pattern="[a-z0-9-]{3,30}" placeholder="my-site"></label>
    <div class="row"><button class="btn primary" id="mk">ساخت برنامه (کارت تأیید)</button><button class="btn" id="dep" disabled>استقرار سورس (کارت تأیید)</button></div><div id="wizProps" class="list"></div></div>`;
  $('#mk').onclick = async () => {
    const name = $('#appName').value.trim(), plan = ($('input[name=plan]:checked') || {}).value;
    try { const r = await api('/api/projects/propose', {method: 'POST', body: {name, plan, platform: a.platform}}); const p = (await api('/api/proposals')).find(x => x.id === r.proposal.id);
      $('#wizProps').insertAdjacentHTML('beforeend', propCard(p)); bindProps(); $('#dep').disabled = false; $$('.steps-wiz span')[2].classList.add('on'); toast('پس از تأیید ساخت برنامه، دکمه‌ی استقرار را بزنید'); } catch (e) { toast(e.message); }
  };
  $('#dep').onclick = async () => {
    try { const r = await api('/api/projects/propose', {method: 'POST', body: {step: 'deploy', name: $('#appName').value.trim(), project_id: a.project_id}}); const p = (await api('/api/proposals')).find(x => x.id === r.proposal.id);
      $('#wizProps').insertAdjacentHTML('beforeend', propCard(p)); bindProps(); $$('.steps-wiz span')[3].classList.add('on'); } catch (e) { toast(e.message); }
  };
}

/* ---------------------------------------------------------------- domains */
VIEWS.domains = async () => {
  const v = $('#view'); const ov = S.ov || await api('/api/overview');
  const apps = ov.apps.filter(a => a.role !== 'off' && a.role !== 'self');
  v.innerHTML = `<div class="grid g2"><div class="card form"><h3>افزودن دامنه‌ی اختصاصی</h3>
    <div class="muted small">نگهبان دامنه را از طریق API به برنامه وصل می‌کند و دقیقاً می‌گوید چه رکوردهایی را در سرویس DNS خود (ایرنیک، آروان‌کلاد، کلادفلر…) ثبت کنید. ثبت رکورد در DNS بیرون از لیارا با API ممکن نیست و باید توسط شما انجام شود.</div>
    <label>برنامه<select id="dApp">${apps.map(a => `<option>${esc(a.name)}</option>`).join('')}</select></label>
    <label>دامنه<input id="dName" class="ltr" placeholder="shop.example.ir"></label>
    <button class="btn primary" id="dAdd">افزودن (کارت تأیید)</button><div id="dProps" class="list"></div></div>
    <div class="card"><h3>دامنه‌های فعلی</h3><div id="dList">…</div></div></div>
    <div class="card" style="margin-top:16px"><h3>راهنمای دامنه‌های .ir (ایرنیک)</h3><ol class="small"><li>اگر می‌خواهید DNS را به لیارا بسپارید، در لیارا یک «ناحیه‌ی DNS» بسازید؛ nameserverهای اختصاصی آن نمایش داده می‌شود.</li><li>در nic.ir وارد شوید → دامنه → «تغییر سرورهای نام» و nameserverها را وارد کنید (تا ۲۴ ساعت زمان می‌برد).</li><li>یا اگر DNS نزد سرویس دیگری است (مثل آروان)، سه رکورد جدول بالا را همان‌جا ثبت کنید.</li><li>بعد از فعال شدن دامنه، SSL رایگان با یک تأیید فعال می‌شود.</li></ol></div>`;
  $('#dAdd').onclick = async () => { try { const r = await api('/api/domains/propose', {method: 'POST', body: {app: $('#dApp').value, domain: $('#dName').value}}); const p = (await api('/api/proposals')).find(x => x.id === r.proposal.id); $('#dProps').insertAdjacentHTML('afterbegin', propCard(p)); bindProps(); } catch (e) { toast(e.message); } };
  try { const ds = await api('/api/domains'); $('#dList').innerHTML = ds.length ? ds.map(domHtml).join('') : '<div class="muted">هنوز دامنه‌ای اضافه نشده؛ برنامه‌ها روی زیردامنه‌ی پیش‌فرض <code>*.liara.run</code> در دسترس‌اند.</div>'; }
  catch (e) { $('#dList').textContent = e.message; }
};
function domHtml(d) {
  return `<div class="item"><div class="row"><b class="ltr">${esc(d.name)}</b><span class="chip ${d.status === 'ACTIVE' ? 'ok' : 'warn'}">${esc(d.status || '؟')}</span><span class="chip">SSL: ${esc(d.certificatesStatus || '—')}</span></div>
  <div class="tablewrap"><table><tr><th>نوع</th><th>نام</th><th>مقدار</th><th>کاربرد</th></tr>${(d.records || []).map(r => `<tr><td>${esc(r.type)}</td><td class="mono">${esc(r.name)}</td><td class="mono">${esc(r.value)}</td><td class="small">${esc(r.why)}</td></tr>`).join('')}</table></div></div>`;
}

/* ---------------------------------------------------------------- scenarios */
VIEWS.scenarios = async () => {
  const v = $('#view'); const sc = await api('/api/scenarios');
  v.innerHTML = `<div class="card small muted" style="margin-bottom:14px">این سناریوها فقط روی برنامه‌ی نمایشی <b class="ltr">${esc(sc.app)}</b> خطای واقعی ایجاد می‌کنند تا چرخه‌ی کامل «تشخیص ← تحلیل ← پیشنهاد ← تأیید ← رفع ← راستی‌آزمایی» را ببینید. پایشگر حداکثر در یک تا دو دقیقه مشکل را پیدا می‌کند.</div>
  ${sc.load.active ? `<div class="chip info" style="margin-bottom:10px">📈 ترافیک مصنوعی فعال: ${fa(sc.load.sent)} درخواست، میانگین پاسخ ${fa(sc.load.avg_latency_ms)}ms، ${fa(Math.round(sc.load.seconds_left / 60))} دقیقه باقی‌مانده</div>` : ''}
  <div class="grid g3">${Object.entries(sc.scenarios).map(([k, s]) => `<div class="card scen"><div class="ic">${s.icon}</div><b>${esc(s.title)}</b><div class="small muted">${esc(s.desc)}</div>
    ${s.deploys ? '<div class="small chip warn">یک استقرار از سهمیه‌ی روزانه مصرف می‌کند</div>' : ''}<button class="btn ${k === 'reset' ? '' : 'primary'}" data-sc="${k}">اجرا</button></div>`).join('')}</div>`;
  $$('[data-sc]').forEach(b => b.onclick = async () => { b.disabled = true; try { const r = await api('/api/scenarios/' + b.dataset.sc, {method: 'POST'}); toast(r.note || 'اجرا شد ✓', 6000); } catch (e) { toast(e.message); } setTimeout(() => b.disabled = false, 4000); });
};

/* ---------------------------------------------------------------- audit + limits + more */
const ACT_FA = {propose: 'پیشنهاد', approve: 'تأیید', reject: 'رد', verify: 'راستی‌آزمایی', restart_app: 'ری‌استارت', resize_app: 'تغییر پلن', set_env: 'تنظیم متغیر', login: 'ورود', login_failed: 'ورود ناموفق',
  incident_open: 'رخداد جدید', policy_update: 'تغییر سیاست', prepare_fix: 'آماده‌سازی وصله', apply_code_fix: 'اعمال وصله', rollback_release: 'بازگشت نسخه', autoscale_down: 'کاهش خودکار پلن', diagnose: 'عیب‌یابی', chat_blocked: 'بستن گفتگو'};
VIEWS.audit = async () => {
  const v = $('#view'); const [rows, J] = await Promise.all([api('/api/audit'), api('/api/jobs').catch(() => null)]);
  const JS = {queued: 'در صف', running: 'در حال اجرا', retrying: 'تلاش دوباره', succeeded: 'موفق', dead: 'مرده (dead-letter)', discarded: 'کنار گذاشته'};
  const jobs = J ? `<div class="card" style="margin-bottom:16px"><h3>صف کارهای نگهبان و مدارشکن‌ها</h3>
    <div class="row small">${Object.entries(J.stats).map(([k, n]) => `<span class="chip ${k === 'dead' ? 'bad' : k === 'succeeded' ? 'ok' : 'info'}">${esc(JS[k] || k)}: ${fa(n)}</span>`).join('') || '<span class="muted">صف خالی است</span>'}
      ${J.circuits.map(c => `<span class="chip ${c.circuit === 'closed' ? 'ok' : 'bad'}">⚡ ${esc(c.upstream)}: ${esc(c.circuit)}</span>`).join('')}</div>
    ${J.jobs.filter(j => j.state === 'dead').length ? `<div class="tablewrap" style="margin-top:8px"><table><tr><th>کار</th><th>نوع</th><th>خطا</th><th>زمان</th><th></th></tr>${J.jobs.filter(j => j.state === 'dead').map(j => `<tr><td class="mono">${esc(j.id)}</td><td>${esc(j.kind)}</td><td class="small">${esc(j.last_error || '')}</td><td class="small">${dt(j.updated_at)}</td>
      <td><button class="btn sm" data-jr="${esc(j.id)}">اجرای دوباره</button> <button class="btn sm ghost" data-jd="${esc(j.id)}">کنار گذاشتن</button></td></tr>`).join('')}</table></div>` : '<div class="muted small" style="margin-top:6px">کار مرده‌ای وجود ندارد ✅</div>'}</div>` : '';
  v.innerHTML = jobs + `<div class="card"><div class="tablewrap"><table><tr><th>زمان</th><th>عامل</th><th>عمل</th><th>برنامه</th><th>نتیجه</th><th>جزئیات</th></tr>
  ${rows.map(r => `<tr><td class="small">${dt(r.ts)}</td><td>${esc(r.actor)}${['autopilot', 'orchestrator', 'negahban', 'watchdog'].includes(r.actor) ? ' <span class="chip small">⚙️</span>' : r.actor === 'agent' ? ' <span class="chip info small">🧠</span>' : ''}</td><td><b>${esc(ACT_FA[r.action] || r.action)}</b></td><td class="ltr">${esc(r.app || '')}</td><td>${r.ok ? '✅' : '❌'}</td><td class="small mono" style="max-width:420px;overflow:hidden;text-overflow:ellipsis">${esc(JSON.stringify(r.detail)).slice(0, 220)}</td></tr>`).join('')}</table></div></div>`;
  $$('[data-jr]').forEach(b => b.onclick = async () => { b.disabled = true; try { const r = await api(`/api/jobs/${b.dataset.jr}/replay`, {method: 'POST'}); toast(r.replayed_as_proposal ? 'کارت تأیید جدید ساخته شد' : 'دوباره در صف قرار گرفت'); VIEWS.audit(); } catch (e) { toast(e.message); b.disabled = false; } });
  $$('[data-jd]').forEach(b => b.onclick = async () => { b.disabled = true; try { await api(`/api/jobs/${b.dataset.jd}/discard`, {method: 'POST'}); VIEWS.audit(); } catch (e) { toast(e.message); b.disabled = false; } });
};
VIEWS.memory = async () => {
  const v = $('#view'); const rows = await api('/api/memory');
  const KF = {fact: 'واقعیت', preference: 'ترجیح', incident: 'رخداد گذشته'};
  v.innerHTML = `<div class="card small muted" style="margin-bottom:14px">حافظه‌ی بلندمدت نگهبان: چیزهایی که از شما یاد گرفته (در گفتگو بگویید «به خاطر بسپار که…») و علت و راه‌حل رخدادهای گذشته که در تشخیص‌های بعدی استفاده می‌شود.</div>
  <form class="card form" id="memForm" style="margin-bottom:14px"><div class="row"><select id="memKind" style="width:auto"><option value="fact">واقعیت</option><option value="preference">ترجیح</option></select>
    <input id="memText" class="grow" placeholder="مثلاً: پروژه‌ی اصلی من bazargard-ai است" maxlength="500"><button class="btn primary" type="submit">افزودن</button></div></form>
  <div class="list">${rows.map(r => `<div class="item"><div class="row"><span class="chip ${r.kind === 'incident' ? 'warn' : 'info'}">${esc(KF[r.kind] || r.kind)}</span>${r.app ? `<span class="chip ltr">${esc(r.app)}</span>` : ''}<span class="muted small right">${dt(r.updated_at)}</span></div>
    <div data-memtext="${esc(r.id)}">${esc(r.text)}</div><div class="row"><button class="btn sm" data-memedit="${esc(r.id)}">ویرایش</button><button class="btn sm ghost" data-memdel="${esc(r.id)}">حذف</button></div></div>`).join('') || '<div class="empty card">هنوز چیزی در حافظه نیست</div>'}</div>`;
  $('#memForm').onsubmit = async e => { e.preventDefault(); const t = $('#memText').value.trim(); if (!t) return; try { await api('/api/memory', {method: 'POST', body: {kind: $('#memKind').value, text: t}}); toast('ذخیره شد'); VIEWS.memory(); } catch (er) { toast(er.message); } };
  $$('[data-memdel]').forEach(b => b.onclick = async () => { if (!confirm('این مورد از حافظه حذف شود؟')) return; b.disabled = true; try { await api('/api/memory/' + b.dataset.memdel, {method: 'DELETE'}); VIEWS.memory(); } catch (er) { toast(er.message); b.disabled = false; } });
  $$('[data-memedit]').forEach(b => b.onclick = async () => { const el = $(`[data-memtext="${b.dataset.memedit}"]`); const t = prompt('متن جدید:', el.textContent); if (!t) return; try { await api('/api/memory/' + b.dataset.memedit, {method: 'PUT', body: {text: t}}); VIEWS.memory(); } catch (er) { toast(er.message); } });
};
VIEWS.limits = async () => {
  const v = $('#view'); const [L, li] = await Promise.all([api('/api/limitations'), api('/api/llm')]);
  const g = li.gate || {};
  v.innerHTML = `<div class="card small muted" style="margin-bottom:14px">نگهبان صادقانه می‌گوید چه کارهایی از طریق API لیارا ممکن نیست و در هر مورد چه جایگزینی دارد.</div>
  <div class="card" style="margin-bottom:16px"><h3>🧠 وضعیت دروازه‌ی مدل زبانی (Anthropic)</h3><div class="facts">
    <div class="fact"><small>پایان پنجره‌ی دسترسی</small><span class="ltr">${esc(g.window_end || '—')}</span></div><div class="fact"><small>آخرین پاسخ</small>${g.last_status ? 'HTTP ' + fa(g.last_status) : '—'}</div>
    <div class="fact"><small>آخرین موفقیت</small>${g.last_ok ? ago(g.last_ok) : '—'}</div><div class="fact"><small>وضعیت</small>${g.last_error ? `<span class="err">${esc(g.last_error)}</span>` : 'عادی'}</div></div>
    <div class="muted small">پس از پایان پنجره (۴۰۳) یا فعال شدن قطع اضطراری (۵۰۳)، نگهبان خودکار به BoxAPI جابه‌جا می‌شود.</div></div><div class="grid g2">${L.map(x => `<div class="card"><h3>⚠️ ${esc(x.title)}</h3><div>${esc(x.text)}</div></div>`).join('')}</div>`;
};
VIEWS.more = () => {
  $('#view').innerHTML = `<div class="grid g3">${['wizard', 'reports', 'alerts', 'autopilot', 'templates', 'servers', 'terminal', 'logs', 'memory', 'domains', 'scenarios', 'audit', 'limits'].map(k => `<button class="card btn" style="justify-content:center;padding:18px" data-act="go" data-arg="${k}">${esc(TITLES[k])}</button>`).join('')}
  <button class="card btn" style="justify-content:center;padding:18px" data-act="logout">خروج</button></div>`;
};

/* ---------------------------------------------------------------- boot */
async function boot() {
  try { await api('/api/me'); } catch { showLogin(); return; }
  $('#login').classList.add('hidden'); $('#app').classList.remove('hidden'); connect();
  const [v, a] = location.hash.slice(1).split('/'); go(v && VIEWS[v] ? v : 'overview', a);
  setInterval(() => { if (S.view === 'overview') VIEWS.overview(true); refreshCounts(); }, 60000);
}
window.go = go;
/* extension views (templates.js, terminal.js, logs.js, ...) register via window.Ngh.VIEWS[name] = async (arg) => {...} */
window.Ngh = {api, esc, fa, pct, toman, ago, clock, dt, toast, modal, propCard, bindProps, go, VIEWS, TITLES, md, S, ACT_FA};
document.addEventListener('click', e => {
  const el = e.target.closest('[data-act]'); if (!el) return;
  const a = el.dataset.act, arg = el.dataset.arg;
  if (a === 'go') go(arg); else if (a === 'app') go('app', arg); else if (a === 'diag') runDiag(arg, el);
  else if (a === 'inc') openIncident(arg); else if (a === 'resolve') resolveInc(arg); else if (a === 'logout') $('#logout').click();
});
boot();
