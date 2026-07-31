#!/usr/bin/env python3
"""端到端验收脚本（对照 08 文档关键条目）。"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get(
    'CHENGZHU_API',
    os.environ.get('CHENZGZU_API', 'http://127.0.0.1:5001'),
)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
BACKEND = os.path.join(ROOT, 'backend')
VENV_SITE = os.path.join(BACKEND, '.venv', 'lib')
# 优先使用项目 venv 的 site-packages
if os.path.isdir(VENV_SITE):
    for name in os.listdir(VENV_SITE):
        if name.startswith('python'):
            site = os.path.join(VENV_SITE, name, 'site-packages')
            if os.path.isdir(site):
                sys.path.insert(0, site)
sys.path.insert(0, BACKEND)


def http(method, path, body=None, timeout=60):
    data = None
    headers = {'Content-Type': 'application/json'}
    if body is not None:
        data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8')
        ct = resp.headers.get('Content-Type', '')
        if 'json' in ct:
            return json.loads(raw)
        return raw


def ok(name, cond, detail=''):
    status = 'PASS' if cond else 'FAIL'
    print(f'[{status}] {name}' + (f' — {detail}' if detail else ''))
    return bool(cond)


def main():
    fails = 0

    # health
    try:
        h = http('GET', '/api/health')
        fails += not ok('health', h.get('status') == 'ok')
    except Exception as e:
        fails += not ok('health', False, str(e))
        print('Backend down; abort')
        sys.exit(1)

    # memory prefill
    pref = http('GET', '/api/memory/prefill')
    fails += not ok('memory.prefill', pref.get('success'), str(pref.get('data')))

    # create + confirm short task (reuse existing completed if create slow)
    created = http('POST', '/api/task/create', {'requirement': '整理贵州茅台近期公告摘要'})
    fails += not ok('task.create', created.get('success'), created.get('error', ''))
    tid = (created.get('data') or {}).get('task_id')
    card = (created.get('data') or {}).get('task_card')
    if tid and card:
        card['analysis_mode'] = os.environ.get('ACCEPTANCE_ANALYSIS_MODE', 'direct')
        conf = http('POST', f'/api/task/{tid}/confirm', {'task_card': card})
        fails += not ok('task.confirm', conf.get('success'))
        run_id = (conf.get('data') or {}).get('run_id')
        fails += not ok('task.confirm.run_id', bool(run_id), str(run_id))
        # wait pipeline (up to 180s)
        done = False
        for _ in range(90):
            suffix = f'?run_id={run_id}' if run_id else ''
            st = http('GET', f'/api/task/{tid}/status{suffix}')
            s = (st.get('data') or {}).get('status')
            if s in ('completed', 'completed_partial', 'failed'):
                done = s.startswith('completed')
                fails += not ok('pipeline.finish', done, s)
                break
            time.sleep(2)
        else:
            fails += not ok('pipeline.finish', False, 'timeout')

        if done:
            suffix = f'?run_id={run_id}' if run_id else ''
            rep = http('GET', f'/api/report/{tid}{suffix}')
            md = (rep.get('data') or {}).get('markdown') or ''
            fails += not ok('report.has_disclaimer', '不构成任何投资建议' in md)
            fails += not ok('report.has_sources', '信息来源清单' in md)
            # citations
            import re
            cites = set(re.findall(r'\[E(\d+)\]', md))
            fails += not ok('report.has_citations', len(cites) > 0, f'n={len(cites)}')
            evidence = http('GET', f'/api/task/{tid}/evidence{suffix}')
            valid_ids = {
                str(item.get('display_id') or '').lstrip('E')
                for item in ((evidence.get('data') or {}).get('items') or [])
            }
            fails += not ok('report.invalid_citations_zero', cites.issubset(valid_ids))
            blacklist = re.compile(r'建议买入|建议卖出|目标价|看多|看空|Alpha\s*信号', re.I)
            fails += not ok('report.advice_blacklist_zero', not blacklist.search(md))
            detail = (st.get('data') or {}).get('progress_detail') or {}
            fails += not ok(
                'pipeline.elapsed_le_480s',
                float(detail.get('elapsed_seconds') or 0) <= 480,
                str(detail.get('elapsed_seconds')),
            )
            fails += not ok(
                'pipeline.cost_le_2cny',
                float(detail.get('llm_cost_cny') or 0) <= 2,
                str(detail.get('llm_cost_cny')),
            )
            runs = http('GET', f'/api/task/{tid}/runs')
            fails += not ok(
                'task.runs.contains_run',
                any(item.get('run_id') == run_id for item in (runs.get('data') or [])),
            )

            # feedback reflection
            fb = http('POST', '/api/feedback/section', {
                'task_id': tid, 'run_id': run_id, 'section_index': 0,
                'vote': 'down', 'comment': '只要表格',
            })
            fails += not ok('feedback.section', fb.get('success'))
            time.sleep(1)
            pb = http('GET', '/api/memory/playbook')
            rules = pb.get('data') or []
            fails += not ok('playbook.candidate', any(r.get('rule_type') == 'style' for r in rules), f'n={len(rules)}')

            # tracking
            sub = http('POST', '/api/tracking/subscribe', {'task_id': tid, 'cron': 'weekly'})
            sid = (sub.get('data') or {}).get('sub_id')
            fails += not ok('tracking.subscribe', bool(sid))
            if sid:
                brief = http('POST', f'/api/tracking/{sid}/run-now')
                fails += not ok('tracking.run_now', brief.get('success'))

            # scenario
            sc = http('POST', '/api/scenario/create', {
                'task_id': tid,
                'run_id': run_id,
                'hypothesis': '假设公司发布业绩预告低于市场预期',
            })
            scen_id = (sc.get('data') or {}).get('scenario_id')
            fails += not ok('scenario.create', bool(scen_id))
            if scen_id:
                http('POST', f'/api/scenario/{scen_id}/start', {})
                for _ in range(30):
                    st = http('GET', f'/api/scenario/{scen_id}/status')
                    if (st.get('data') or {}).get('status') == 'completed':
                        break
                    time.sleep(1)
                rp = http('GET', f'/api/scenario/{scen_id}/report')
                smd = (rp.get('data') or {}).get('markdown') or ''
                fails += not ok('scenario.report', '模拟' in smd or '沙盘' in smd or '情景' in smd)

    # unit-ish local graph
    from app.utils.graph_client import GraphClient
    g = GraphClient('project_verify_tmp')
    g.delete_group()
    g = GraphClient('project_verify_tmp')
    g.add_episode('验证净利润披露', reference_time='2026-01-01', meta={'card_id': 1})
    hits = g.quick_search('净利润')
    stats = g.get_graph_statistics()
    fails += not ok('graph.local', bool(hits) and stats.get('nodes', 0) > 0, str(stats))
    g.delete_group()

    print('—' * 40)
    if fails:
        print(f'FAILED checks: {fails}')
        sys.exit(1)
    print('ALL ACCEPTANCE CHECKS PASSED')
    sys.exit(0)


if __name__ == '__main__':
    main()
