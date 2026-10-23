'use strict';
/* Diagnostics views: «ترمینال» (per-app container terminal + quick checks + runbooks) and «کاوشگر لاگ» (log explorer with
   error groups, slow routes, LLM usage and request traces). CSP-safe: no inline handlers; every server string goes through esc(). */
(() => {
  const N = window.Ngh; if (!N) return;
  const {api, esc, fa, toast, modal, propCard, bindProps} = N;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
  N.TITLES.terminal = 'ترمینال و عیب‌یابی'; N.TITLES.logs = 'کاوشگر لاگ';
  const D = {apps: null, app: localStorage.getItem('ngh_diag_app') || '', hist: [], hi: 0};
  const CHECKS = {overview: 'نمای کلی', processes: 'پروسه‌ها', memory: 'حافظه', cpu: 'CPU', disk: 'دیسک', data_usage: 'حجم /data', ports: 'پورت‌ها و اتصال‌ها',
    env_names: 'نام متغیرها', http_local: 'درخواست از داخل', sqlite_check: 'سلامت SQLite', log_tracebacks: 'خطاهای فایل لاگ', open_fds: 'فایل‌های باز', runtime_info: 'نسخه‌ها و بسته‌ها', limits: 'محدودیت‌ها'};
  const RUNBOOKS = {auto: 'تشخیص خودکار', oom: 'کمبود حافظه / OOM', crash_loop: 'کرش و ری‌استارت پیاپی', slow_responses: 'کندی پاسخ‌ها', build_failure: 'شکست بیلد',
    missing_env: 'متغیر محیطی گمشده', disk_full: 'پر شدن دیسک', tls_problem: 'مشکل TLS/SSL', dependency_outage: 'قطعی سرویس وابسته'};
  const SEV = {critical: 'bad', warn: 'warn', info: 'info'};
  const VCHIP = {read: 'ok', sensitive: 'warn', blocked: 'bad'};

  if (!$('#diagCss')) {
    const st = document.createElement('style'); st.id = 'diagCss';
    st.textContent = `.term{background:#0b1220;color:#cbd5e1;border-radius:12px;padding:10px 12px;font-family:var(--mono);font-size:12.5px;min-height:220px;max-height:460px;overflow:auto;direction:ltr;text-align:left;white-space:pre-wrap;word-break:break-word}
      .term .p{color:#5eead4}.term .m{color:#94a3b8}.term .e{color:#fca5a5}.term .w{color:#fde68a}.termrow{display:flex;gap:8px;margin-top:8px;direction:ltr}.termrow input{font-family:var(--mono)}
      .dg-checks{display:flex;flex-wrap:wrap;gap:6px}.dg-kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:6px;margin:6px 0}
      .dg-kv div{background:var(--bg,#f6f7fb);border-radius:8px;padding:6px 8px;font-size:12.5px}.dg-kv small{display:block;color:var(--muted,#64748b)}
      .dg-h{border-inline-start:4px solid var(--line);padding:6px 10px;margin:6px 0;border-radius:6px;background:#fff}.dg-h.top{border-color:var(--brand)}
      .dg-tbl{width:100%;border-collapse:collapse;font-size:12.5px}.dg-tbl th,.dg-tbl td{padding:5px 7px;border-bottom:1px solid var(--line);text-align:start;vertical-align:top}
      .dg-tbl td.ltr,.dg-tbl .ltr{direction:ltr;text-align:left;font-family:var(--mono)}`;
    document.head.appendChild(st);
  }

  async function busy(btn, text, fn) {
    if (!btn || btn.disabled) return null;
    const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<span class="spin"></span> ${esc(text)}`;
    try { return await fn(); } catch (e) { toast(e.message, 6500); return null; } finally { if (btn.isConnected) { btn.disabled = false; btn.innerHTML = old; } }
  }
  async function loadApps() { if (!D.apps) D.apps = await api('/api/diag/apps'); if (!D.app && D.apps.length) D.app = D.apps[0].name; return D.apps; }
  const appSelect = id => `<select id="${id}" class="ltr" style="max-width:240px">${D.apps.map(a => `<option value="${esc(a.name)}" ${a.name === D.app ? 'selected' : ''}>${esc(a.name)}${a.locked ? ' 🔒' : ''}</option>`).join('')}</select>`;
  const kv = o => `<div class="dg-kv">${Object.entries(o).filter(([k, v]) => v !== null && v !== undefined && typeof v !== 'object').slice(0, 24)
    .map(([k, v]) => `<div><small class="ltr">${esc(k)}</small><span class="ltr">${esc(typeof v === 'number' ? v.toLocaleString('en-US') : v)}</span></div>`).join('')}</div>`;
  const pre = o => `<pre class="logbox" style="white-space:pre-wrap">${esc(typeof o === 'string' ? o : JSON.stringify(o, null, 2))}</pre>`;
  const tbl = (rows, cols) => rows && rows.length ? `<div class="tablewrap"><table class="dg-tbl"><tr>${cols.map(c => `<th>${esc(c[1])}</th>`).join('')}</tr>${rows.map(r => `<tr>${cols.map(c => `<td class="${c[2] || ''}">${c[3] ? c[3](r) : esc(r[c[0]] ?? '—')}</td>`).join('')}</tr>`).join('')}</table></div>` : '<div class="muted small">موردی نیست</div>';

  /* ------------------------------------------------------------ terminal */
  function termWrite(html) { const t = $('#term'); if (!t) return; t.insertAdjacentHTML('beforeend', html); t.scrollTop = t.scrollHeight; }
  async function runCommand() {
    const inp = $('#termIn'), cmd = inp.value.trim(); if (!cmd) return;
    D.hist.push(cmd); D.hi = D.hist.length; inp.value = ''; $('#termV').innerHTML = '';
    termWrite(`<span class="p">${esc(D.app)} $</span> ${esc(cmd)}\n`);
    try {
      const r = await api('/api/diag/exec', {method: 'POST', body: {app: D.app, command: cmd}});
      const res = r.result || {};
      if (res.proposal) { termWrite(`<span class="w">⏸ ${esc(r.label)} — کارت تأیید: ${esc(res.proposal.title || res.proposal.id)}</span>\n`); toast('کارت تأیید ساخته شد؛ در بخش تأییدها ببینید'); return; }
      if (res.error) { termWrite(`<span class="e">${esc(res.error)}</span>\n`); return; }
      termWrite(esc(res.output || '') + (res.hint ? `\n<span class="w">${esc(res.hint)}</span>` : '') + `\n<span class="m">— ${fa(res.ms)} ms${res.timed_out ? ' · timeout' : ''}${res.truncated ? ' · truncated' : ''}</span>\n`);
    } catch (e) { termWrite(`<span class="e">${esc(e.message)}</span>\n`); }
  }
  let _clsT = null;
  function previewVerdict() {
    clearTimeout(_clsT); const cmd = $('#termIn').value.trim(); if (!cmd) { $('#termV').innerHTML = ''; return; }
    _clsT = setTimeout(async () => { try { const r = await api('/api/diag/classify?command=' + encodeURIComponent(cmd)); $('#termV').innerHTML = `<span class="chip ${VCHIP[r.verdict]}">${esc(r.label)}</span> <span class="muted small">${esc(r.why)}</span>`; } catch { } }, 350);
  }
  function renderCheck(name, r) {
    if (r.error) return `<div class="err">${esc(r.error)}</div>`;
    const parts = (name === 'overview' ? ['memory', 'cpu', 'disk', 'processes'] : [name]).map(k => {
      const v = r[k] || {}; if (v.error) return `<h4>${esc(CHECKS[k] || k)}</h4><div class="err">${esc(v.error)}</div>`;
      let body = kv(v);
      if (k === 'processes') body += tbl(v.processes, [['pid', 'PID', 'ltr'], ['name', 'نام', 'ltr'], ['rss_mb', 'RAM (MB)', 'ltr'], ['cpu_s', 'CPU (s)', 'ltr'], ['threads', 'رشته', 'ltr'], ['fds', 'fd', 'ltr'], ['cmd', 'فرمان', 'ltr']]);
      else if (k === 'disk') body = tbl(v.mounts, [['mount', 'مسیر', 'ltr'], ['total_mb', 'کل (MB)', 'ltr'], ['avail_mb', 'آزاد (MB)', 'ltr'], ['use_pct', 'استفاده ٪', 'ltr']]);
      else if (k === 'ports') body = tbl(v.listening, [['addr', 'آدرس', 'ltr'], ['port', 'پورت', 'ltr']]) + pre({tcp_states: v.tcp_states, top_remote: v.top_remote});
      else if (k === 'env_names') body = kv({count: v.count}) + `<div class="row small">${(v.names || []).map(n => `<span class="chip ltr">${esc(n)}</span>`).join('')}</div>` + ((v.missing_expected || []).length ? `<div class="err">گمشده: ${esc(v.missing_expected.join(', '))}</div>` : '');
      else if (['sqlite_check', 'data_usage', 'log_tracebacks', 'open_fds', 'runtime_info', 'http_local', 'limits'].includes(k)) body = kv(v) + pre(v);
      return `<h4>${esc(CHECKS[k] || k)}</h4>${body}`;
    });
    return parts.join('') + `<div class="muted small">زمان اجرا: ${fa(r._exec_ms)} ms</div>`;
  }
  function renderRunbook(r) {
    if (r.error) return `<div class="err">${esc(r.error)}</div>`;
    const props = (r.proposals || []).filter(p => p && p.id);
    return `<div class="row"><b>${esc(r.title)}</b>${r.triage ? `<span class="chip info">تشخیص: ${esc(r.triage)}</span>` : ''}<span class="muted small">${fa(r.seconds)} ثانیه</span></div>
      ${(r.findings || []).map(f => `<div><span class="chip ${SEV[f.severity] || 'info'}">${esc(f.severity)}</span> ${esc(f.text)}</div>`).join('')}
      <h4>فرضیه‌ها</h4>${(r.hypotheses || []).map((h, i) => `<div class="dg-h ${i ? '' : 'top'}"><b>${esc(h.title)}</b> <span class="chip">${fa(Math.round(h.confidence * 100))}٪</span>
        <div class="muted small">${(h.evidence || []).map(esc).join(' · ')}</div></div>`).join('')}
      <h4>اقدام‌های پیشنهادی</h4>${tbl(r.recommended_actions, [['tool', 'ابزار', 'ltr', a => esc(a.tool || 'دستی')], ['why', 'چرا', '', a => esc(a.why || a.manual || '')], ['confidence', 'اطمینان', 'ltr', a => fa(Math.round((a.confidence || 0) * 100)) + '٪']])}
      ${props.length ? `<h4>کارت‌های تأیید ساخته‌شده</h4><div id="dgProps">${props.map(p => propCard(p)).join('')}</div>` : ''}
      <div class="small muted">مراحل: ${(r.steps || []).map(s => `${s.ok ? '✅' : '❌'} ${esc(s.title)}`).join(' · ')}</div>`;
  }

  N.VIEWS.terminal = async arg => {
    const v = $('#view'); await loadApps(); if (arg) D.app = arg;
    const lock = (D.apps.find(a => a.name === D.app) || {}).locked;
    v.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="row"><b>برنامه:</b>${appSelect('dgApp')}
        ${lock ? '<span class="chip warn">🔒 فقط فرمان‌های فقط‌خواندنی</span>' : '<span class="chip info">فرمان‌های غیرمجاز ⇐ کارت تأیید</span>'}
        <span class="muted small">بدون shell: بدون pipe، نقل‌قول و redirect؛ آرگومان‌ها با فاصله جدا می‌شوند.</span></div></div>
      <div class="grid g2"><div class="card"><h3>ترمینال کانتینر</h3><div id="term" class="term"><span class="m"># مثال: df -h · cat /proc/meminfo · ls -la /data · tail -n 50 /data/app.log · printenv\n</span></div>
          <div class="termrow"><input id="termIn" autocomplete="off" spellcheck="false" placeholder="command"><button class="btn primary" id="termGo">اجرا</button></div><div id="termV" class="small" style="margin-top:6px"></div></div>
        <div class="card"><h3>بررسی‌های آماده</h3><div class="dg-checks">${Object.entries(CHECKS).map(([k, t]) => `<button class="btn sm" data-chk="${k}">${esc(t)}</button>`).join('')}</div><div id="chkOut" style="margin-top:10px"></div></div></div>
      <div class="card" style="margin-top:14px"><h3>دستورالعمل‌های عیب‌یابی (Runbook)</h3><div class="row"><select id="rbSel" style="max-width:260px">${Object.entries(RUNBOOKS).map(([k, t]) => `<option value="${k}">${esc(t)}</option>`).join('')}</select>
        <label class="switch small"><input type="checkbox" id="rbProp"> ساخت کارت تأیید برای اقدام‌ها</label><button class="btn primary" id="rbGo">اجرا</button></div><div id="rbOut" style="margin-top:10px"></div></div>`;
    $('#dgApp').addEventListener('change', e => { D.app = e.target.value; localStorage.setItem('ngh_diag_app', D.app); N.VIEWS.terminal(); });
    $('#termGo').addEventListener('click', e => busy(e.currentTarget, 'در حال اجرا', runCommand));
    $('#termIn').addEventListener('input', previewVerdict);
    $('#termIn').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); $('#termGo').click(); }
      else if (e.key === 'ArrowUp' && D.hi > 0) { D.hi--; e.target.value = D.hist[D.hi]; }
      else if (e.key === 'ArrowDown') { D.hi = Math.min(D.hist.length, D.hi + 1); e.target.value = D.hist[D.hi] || ''; }
    });
    $$('[data-chk]').forEach(b => b.addEventListener('click', () => busy(b, CHECKS[b.dataset.chk], async () => {
      const r = await api('/api/diag/run', {method: 'POST', body: {tool: 'container_diag', args: {app: D.app, check: b.dataset.chk}}});
      $('#chkOut').innerHTML = renderCheck(b.dataset.chk, r);
    })));
    $('#rbGo').addEventListener('click', e => busy(e.currentTarget, 'در حال جمع‌آوری شواهد', async () => {
      const r = await api('/api/diag/run', {method: 'POST', body: {tool: 'run_runbook', args: {app: D.app, runbook: $('#rbSel').value, propose: $('#rbProp').checked}}});
      $('#rbOut').innerHTML = renderRunbook(r); if ($('#dgProps')) bindProps();
    }));
  };

  /* ------------------------------------------------------------ log explorer */
  N.VIEWS.logs = async arg => {
    const v = $('#view'); await loadApps(); if (arg) D.app = arg;
    v.innerHTML = `<div class="card" style="margin-bottom:14px"><div class="row"><b>برنامه:</b>${appSelect('lgApp')}
        <select id="lgMin" style="max-width:150px">${[[30, '۳۰ دقیقه'], [60, '۱ ساعت'], [180, '۳ ساعت'], [720, '۱۲ ساعت'], [1440, '۲۴ ساعت']].map(([m, t]) => `<option value="${m}" ${m === 60 ? 'selected' : ''}>${t}</option>`).join('')}</select>
        <input id="lgQ" placeholder="جست‌وجو (حداقل ۳ حرف)" style="max-width:240px"><button class="btn primary" id="lgGo">تحلیل</button>
        <input id="lgRid" class="ltr" placeholder="request_id" style="max-width:200px"><button class="btn" id="lgTr">ردگیری درخواست</button></div></div><div id="lgOut"></div>`;
    const load = async () => {
      const q = $('#lgQ').value.trim();
      const r = await api(`/api/diag/logs?app=${encodeURIComponent(D.app)}&minutes=${$('#lgMin').value}${q.length >= 3 ? '&search=' + encodeURIComponent(q) : ''}`);
      if (r.error) { $('#lgOut').innerHTML = `<div class="card err">${esc(r.error)}</div>`; return; }
      const chips = `<div class="row">${[['خط', r.lines], ['درخواست', r.requests], ['خطای 5xx', r.errors_5xx], ['گروه خطا', r.error_group_count], ['ساختاریافته', Math.round((r.structured_ratio || 0) * 100) + '٪']]
        .map(([t, n]) => `<span class="chip info">${esc(t)}: ${esc(typeof n === 'number' ? fa(n) : n)}</span>`).join('')}${r.restart_loop ? '<span class="chip bad">حلقه‌ی ری‌استارت</span>' : ''}${(r.oom || []).length ? `<span class="chip bad">OOM: ${fa(r.oom.length)}</span>` : ''}</div>`;
      $('#lgOut').innerHTML = `<div class="card" style="margin-bottom:14px">${chips}${r._attach && window.NghViz ? NghViz.render(r._attach) : ''}</div>
        <div class="card" style="margin-bottom:14px"><h3>گروه‌های خطا</h3>${tbl(r.error_groups, [['error_type', 'نوع', 'ltr'], ['count', 'تعداد', 'ltr'], ['last_seen', 'آخرین بار', 'ltr'],
          ['fingerprint', 'اثر انگشت', 'ltr', g => `<details><summary class="ltr">${esc(g.fingerprint)}</summary><div class="small">${esc(g.message || '')}</div><pre class="logbox">${esc(g.sample || '')}</pre></details>`],
          ['request_ids', 'درخواست‌ها', 'ltr', g => (g.request_ids || []).map(id => `<button class="btn sm ghost ltr" data-rid="${esc(id)}">${esc(id)}</button>`).join(' ')]])}</div>
        <div class="grid g2"><div class="card"><h3>کندترین مسیرها</h3>${tbl(r.slowest_routes, [['route', 'مسیر', 'ltr'], ['count', 'تعداد', 'ltr'], ['p50_ms', 'p50', 'ltr'], ['p95_ms', 'p95', 'ltr'], ['errors', '5xx', 'ltr']])}</div>
        <div class="card"><h3>مصرف مدل‌های زبانی</h3>${tbl(r.llm, [['model', 'مدل', 'ltr'], ['calls', 'فراخوانی', 'ltr'], ['tokens_in', 'توکن ورودی', 'ltr'], ['tokens_out', 'توکن خروجی', 'ltr'], ['cost', 'هزینه', 'ltr'], ['errors', 'خطا', 'ltr']])}</div></div>
        <div class="card" style="margin-top:14px"><h3>پرترافیک‌ترین مسیرها</h3>${tbl(r.busiest_routes, [['route', 'مسیر', 'ltr'], ['count', 'تعداد', 'ltr'], ['errors', '5xx', 'ltr'], ['p95_ms', 'p95', 'ltr']])}</div>`;
      $$('[data-rid]').forEach(b => b.addEventListener('click', () => trace(b.dataset.rid)));
    };
    const trace = async rid => {
      const r = await api(`/api/diag/trace?app=${encodeURIComponent(D.app)}&request_id=${encodeURIComponent(rid)}`);
      modal(`<h3>ردگیری ${esc(rid)}</h3>${r.error ? `<div class="err">${esc(r.error)}</div>` : `<div class="row small"><span class="chip">${fa(r.count)} رویداد</span><span class="chip ${r.errors ? 'bad' : 'ok'}">${fa(r.errors)} خطا</span>${r.span_ms != null ? `<span class="chip">${fa(r.span_ms)} ms</span>` : ''}</div>
        ${tbl(r.events, [['t', 'زمان', 'ltr'], ['level', 'سطح', 'ltr'], ['event', 'رویداد', 'ltr'], ['route', 'مسیر', 'ltr'], ['status', 'وضعیت', 'ltr'], ['duration_ms', 'ms', 'ltr'], ['msg', 'پیام', 'ltr']])}`}`);
    };
    $('#lgApp').addEventListener('change', e => { D.app = e.target.value; localStorage.setItem('ngh_diag_app', D.app); });
    $('#lgGo').addEventListener('click', e => busy(e.currentTarget, 'در حال تحلیل', load));
    $('#lgTr').addEventListener('click', e => { const id = $('#lgRid').value.trim(); if (id.length >= 6) busy(e.currentTarget, 'در حال ردگیری', () => trace(id)); else toast('شناسه‌ی درخواست کوتاه است'); });
    busy($('#lgGo'), 'در حال تحلیل', load);
  };
})();
