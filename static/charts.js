'use strict';
/* Self-contained SVG charts (line / area / bar / pie) and Mermaid diagrams via the vendored Pele renderer.
   No CDN, no eval: works under CSP script-src 'self'. */
(function () {
  const PAL = ['#0f766e', '#7c3aed', '#1d4ed8', '#d97706', '#db2777', '#059669', '#4b5563', '#dc2626'];
  const e = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const nf = v => Math.abs(v) >= 1000 ? Math.round(v).toLocaleString('fa-IR') : (+v.toFixed(v < 1 ? 3 : 1)).toLocaleString('fa-IR');
  const tl = t => new Date(t * 1000).toLocaleTimeString('fa-IR', {hour: '2-digit', minute: '2-digit'});

  function timeChart(spec) {
    const W = 640, H = 230, L = 52, R = 12, T = 14, B = 30;
    const all = spec.series.flatMap(s => s.points || []);
    if (all.length < 2) return '<div class="empty small">داده‌ی کافی برای نمودار نیست</div>';
    const t0 = Math.min(...all.map(p => p[0])), t1 = Math.max(...all.map(p => p[0]));
    const vmax = Math.max(spec.limit || 0, ...all.map(p => p[1])) * 1.12 || 1;
    const X = t => L + ((t - t0) / ((t1 - t0) || 1)) * (W - L - R), Y = v => T + (1 - v / vmax) * (H - T - B);
    let g = '';
    for (let i = 0; i <= 4; i++) { const v = vmax * i / 4, y = Y(v); g += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="#e5e7eb"/><text x="${L - 6}" y="${y + 4}" font-size="10.5" text-anchor="end" fill="#6b7280">${nf(v)}</text>`; }
    for (let i = 0; i <= 4; i++) { const t = t0 + (t1 - t0) * i / 4; g += `<text x="${X(t)}" y="${H - 10}" font-size="10.5" text-anchor="middle" fill="#6b7280">${tl(t)}</text>`; }
    if (spec.limit) g += `<line x1="${L}" x2="${W - R}" y1="${Y(spec.limit)}" y2="${Y(spec.limit)}" stroke="#ef4444" stroke-dasharray="6 4"/><text x="${W - R - 4}" y="${Y(spec.limit) - 5}" font-size="11" text-anchor="end" fill="#ef4444">${e(spec.limit_label || 'سقف')} (${nf(spec.limit)})</text>`;
    spec.series.forEach((s, k) => {
      const pts = (s.points || []).map(p => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' '), c = PAL[k % PAL.length];
      if (spec.type === 'area') g += `<polygon fill="${c}22" points="${L},${Y(0)} ${pts} ${W - R},${Y(0)}"/>`;
      g += `<polyline fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round" points="${pts}"><title>${e(s.name)}</title></polyline>`;
      const last = s.points[s.points.length - 1]; if (last) g += `<circle cx="${X(last[0])}" cy="${Y(last[1])}" r="3.5" fill="${c}"><title>${e(s.name)}: ${nf(last[1])} ${e(spec.unit || '')}</title></circle>`;
    });
    // svg is drawn LTR (time flows left->right) inside an RTL page
    return `<svg viewBox="0 0 ${W} ${H}" class="vchart" direction="ltr">${g}</svg>${legend(spec.series)}`;
  }
  function barChart(spec) {
    const vals = spec.series[0].values || [], labels = spec.labels || [];
    const W = 640, H = 240, L = 52, B = 46, T = 12, mx = Math.max(...vals, 0) * 1.12 || 1, bw = (W - L - 10) / Math.max(1, vals.length);
    let g = '';
    for (let i = 0; i <= 4; i++) { const v = mx * i / 4, y = T + (1 - v / mx) * (H - T - B); g += `<line x1="${L}" x2="${W - 10}" y1="${y}" y2="${y}" stroke="#e5e7eb"/><text x="${L - 6}" y="${y + 4}" font-size="10.5" text-anchor="end" fill="#6b7280">${nf(v)}</text>`; }
    vals.forEach((v, i) => { const h = (v / mx) * (H - T - B), x = L + i * bw + bw * .15, y = H - B - h;
      g += `<rect x="${x}" y="${y}" width="${bw * .7}" height="${h}" rx="4" fill="${PAL[i % PAL.length]}"><title>${e(labels[i])}: ${nf(v)} ${e(spec.unit || '')}</title></rect><text x="${x + bw * .35}" y="${H - B + 15}" font-size="10.5" text-anchor="middle" fill="#374151">${e(String(labels[i] || '').slice(0, 14))}</text><text x="${x + bw * .35}" y="${y - 4}" font-size="10.5" text-anchor="middle" fill="#111827">${nf(v)}</text>`; });
    return `<svg viewBox="0 0 ${W} ${H}" class="vchart" direction="ltr">${g}</svg>`;
  }
  function lineFromLabels(spec) {
    const vals = spec.series[0].values || [];
    return timeChart({...spec, type: 'line', series: [{name: spec.series[0].name, points: vals.map((v, i) => [i, v])}]}).replace(/<text x="[^"]+" y="\d+" font-size="10.5" text-anchor="middle" fill="#6b7280">[^<]*<\/text>/g, '');
  }
  function pieChart(spec) {
    const items = spec.series.filter(s => s.value > 0), tot = items.reduce((a, s) => a + s.value, 0) || 1;
    let a0 = -Math.PI / 2, g = '';
    const cx = 110, cy = 110, r = 95;
    items.forEach((s, i) => { const a1 = a0 + 2 * Math.PI * s.value / tot, big = a1 - a0 > Math.PI ? 1 : 0;
      const p = items.length === 1 ? `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${PAL[0]}"/>` : `<path d="M${cx},${cy} L${cx + r * Math.cos(a0)},${cy + r * Math.sin(a0)} A${r},${r} 0 ${big} 1 ${cx + r * Math.cos(a1)},${cy + r * Math.sin(a1)} Z" fill="${PAL[i % PAL.length]}" stroke="#fff" stroke-width="2"><title>${e(s.name)}: ${nf(s.value)} ${e(spec.unit || '')}</title></path>`;
      g += p; a0 = a1; });
    g += `<circle cx="${cx}" cy="${cy}" r="48" fill="#fff"/><text x="${cx}" y="${cy + 5}" font-size="14" font-weight="700" text-anchor="middle">${nf(tot)}</text>`;
    const leg = items.map((s, i) => `<div class="lg"><i style="background:${PAL[i % PAL.length]}"></i><span class="ltr">${e(s.name)}</span><b>${nf(s.value)}</b><small>${Math.round(100 * s.value / tot).toLocaleString('fa-IR')}٪</small></div>`).join('');
    return `<div class="pie"><svg viewBox="0 0 220 220" class="vpie">${g}</svg><div class="pie-leg">${leg}</div></div>`;
  }
  function legend(series) { return series.length > 1 ? `<div class="legend row">${series.map((s, k) => `<span class="lg"><i style="background:${PAL[k % PAL.length]}"></i>${e(s.name)}</span>`).join('')}</div>` : ''; }

  function sanitizeSvg(svg) {
    const doc = new DOMParser().parseFromString(svg, 'image/svg+xml');
    if (doc.querySelector('parsererror')) return null;
    doc.querySelectorAll('script, foreignObject, iframe, object, embed').forEach(n => n.remove());
    doc.querySelectorAll('*').forEach(n => [...n.attributes].forEach(a => { if (/^on/i.test(a.name) || /javascript:/i.test(a.value)) n.removeAttribute(a.name); }));
    return new XMLSerializer().serializeToString(doc.documentElement);
  }
  function diagram(spec) {
    if (!window.pele || !window.pele.render) return `<pre class="mono small">${e(spec.mermaid)}</pre>`;
    try { const out = window.pele.render(spec.mermaid); const svg = sanitizeSvg(out.svg || out); if (!svg) throw new Error('svg');
      return `<div class="pele diag">${svg}</div><details class="small"><summary>کد Mermaid</summary><pre class="mono">${e(spec.mermaid)}</pre></details>`; }
    catch (err) { return `<div class="small err">نمودار قابل رسم نبود (${e(err.message || err)})</div><pre class="mono small">${e(spec.mermaid)}</pre>`; }
  }
  function render(att) {
    let body = '';
    if (att.kind === 'chart') body = att.type === 'pie' ? pieChart(att) : att.type === 'bar' ? barChart(att) : (att.series[0] && att.series[0].points) ? timeChart(att) : lineFromLabels(att);
    else if (att.kind === 'diagram') body = diagram(att);
    else if (att.kind === 'logs') body = `<div class="logbox">${(att.lines || []).map(l => `<div class="${/error|traceback|exception|critical| 5\d\d /i.test(l) ? 'e' : /warn|slow/i.test(l) ? 'w' : ''}">${e(l)}</div>`).join('') || 'لاگی نیست'}</div>`;
    return `<div class="attach"><div class="attach-t">${att.kind === 'diagram' ? '🗺️' : att.kind === 'logs' ? '🧾' : '📊'} ${e(att.title || '')}${att.unit ? ` <span class="muted small">(${e(att.unit)})</span>` : ''}</div>${body}</div>`;
  }
  window.NghViz = {render, timeChart};
})();
