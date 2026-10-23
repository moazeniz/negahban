"""Chat tools for the daily health report and LLM usage."""
from .tools import tool


@tool('daily_report', 'read', 'Daily health report ("گزارش امروز"): per-app uptime/latency/resources, incidents and fixes, autopilot actions, cost and credit runway, LLM usage, recommendations. generate=true builds a fresh one now.',
      {'generate': {'type': 'boolean'}}, [], label_fa='در حال آماده‌سازی گزارش روزانه…')
async def daily_report(a, ctx):
    from . import reports
    rep = (await reports.build(origin='chat')) if a.get('generate') or not reports.get() else reports.get()
    apps = [{k: x[k] for k in ('app', 'uptime_pct', 'latency_p95_ms', 'cpu_peak_pct', 'mem_peak_pct', 'toman_per_hour')} | {'incidents': len(x['incidents']), 'autopilot': len(x['autopilot_actions'])} for x in rep['apps']]
    return {'id': rep['id'], 'day': rep['day'], 'summary': rep['summary'], 'apps': apps, 'cost': {k: rep['cost'].get(k) for k in ('balance_toman', 'total_toman_per_hour', 'hours_left')},
            'llm': rep['llm'], 'recommendations': rep['recommendations'], 'view': '/#reports'}


@tool('llm_usage_report', 'read', "Negahban's own LLM usage per day/feature: calls, tokens, prompt-cache hit rate and estimated cost.", {'days': {'type': 'integer', 'minimum': 1, 'maximum': 30}}, [],
      label_fa='در حال خواندن مصرف مدل زبانی…')
async def llm_usage_report(a, ctx):
    from .llm import usage_rows
    return usage_rows(int(a.get('days') or 7))
