'use strict';
/* Multi-team: header switcher (name + balance), per-team overview cards, test-team accent. Every API call carries the selected
   team in X-Ngh-Team (see api() in app.js); approval cards show their own team (p.team) regardless of the selection.
   CSP-safe: no inline handlers; every value escaped. */
(function () {
  const N = window.Ngh, $ = (s, r = document) => r.querySelector(s);
  const {api, esc, fa, VIEWS, S} = N;
  const T = v => v == null ? '—' : fa(Math.round(v)) + ' تومان';
  let last = null;

  function setTeam(id) {
    S.team = id || '';
    try { localStorage.setItem('ngh_team', S.team); } catch { }
    accent();
    N.go(S.view || 'overview', S.arg);
    render(true);
  }
  function selected() {
    if (!last) return null;
    return last.teams.find(t => t.id === (S.team || last.default)) || last.teams[0];
  }
  function accent() {
    const t = selected();
    document.body.classList.toggle('team-test', !!(t && t.test));
  }
  async function render(fresh) {
    const box = $('#teamSwitch'); if (!box) return;
    try { last = await api('/api/teams' + (fresh ? '?fresh=1' : '')); } catch { return; }
    if (!last.teams || last.teams.length < 2) { box.innerHTML = ''; return; }
    if (S.team && !last.teams.some(t => t.id === S.team)) S.team = '';
    const cur = selected();
    box.innerHTML = `<span class="chip" style="border-color:${esc(cur.color)};color:${esc(cur.color)};font-weight:800">${cur.test ? '🧪' : '🏢'} ${esc(cur.label_fa)}</span>
      <select id="teamSel" aria-label="انتخاب تیم لیارا">${last.teams.map(t => `<option value="${esc(t.id)}" ${t.id === cur.id ? 'selected' : ''}>${esc(t.name)} — ${esc(T(t.balance))}</option>`).join('')}</select>`;
    $('#teamSel').addEventListener('change', e => setTeam(e.target.value === last.default ? '' : e.target.value));
    accent();
  }

  const ov = VIEWS.overview;
  VIEWS.overview = async (silent) => {
    await ov(silent);
    if (N.S.view !== 'overview') return;
    await render(false);
    if (!last || last.teams.length < 2 || $('#xTeams')) return;
    const cur = selected();
    const g = document.createElement('div'); g.id = 'xTeams'; g.className = 'teams-grid';
    g.innerHTML = last.teams.map(t => `<div class="card team-card ${t.id === cur.id ? 'sel' : ''}" style="--tc:${esc(t.color)}">
        <div class="row"><b>${t.test ? '🧪' : '🏢'} ${esc(t.label_fa)}</b>${t.id === cur.id ? '<span class="chip ok">نمایش داده می‌شود</span>' : `<button class="btn sm" data-team="${esc(t.id)}">نمایش این تیم</button>`}</div>
        <div class="facts"><div class="fact"><small>موجودی</small>${esc(T(t.balance))}</div><div class="fact"><small>هزینه‌ی ساعتی</small>${esc(T(t.burn_toman_h))}</div>
        <div class="fact"><small>برنامه‌ها</small>${t.apps == null ? '—' : fa(t.apps)}</div>
        <div class="fact"><small>حاشیه‌ی قانون یک‌هفته</small>${t.week_headroom == null ? '—' : `<span style="color:${t.week_headroom < 200000 ? '#b91c1c' : 'inherit'}">${esc(T(t.week_headroom))}</span>`}</div></div>
        ${t.error ? `<div class="err small">${esc(t.error)}</div>` : ''}</div>`).join('');
    const v = $('#view'); v.insertBefore(g, v.firstChild);
    g.querySelectorAll('[data-team]').forEach(b => b.addEventListener('click', () => setTeam(b.dataset.team === last.default ? '' : b.dataset.team)));
  };
  N.setTeam = setTeam;
})();
