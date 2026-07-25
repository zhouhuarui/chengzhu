"""T0.3 SQLite 层验收测试"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config


def setup_module(_module=None):
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    Config.DB_PATH = path
    Config.UPLOAD_FOLDER = os.path.dirname(path)
    # 重置线程本地连接
    from app.utils import db as dbmod
    if getattr(dbmod._local, 'conn', None):
        dbmod._local.conn.close()
        dbmod._local.conn = None
    dbmod.init_db()


def test_task_run_insert_and_get():
    from app.utils.db import insert_task_run, get_task_run
    insert_task_run(
        run_id='run_test_1',
        task_id='task_1',
        task_card={'deliverable': 'summary', 'symbols': [{'code': '600519'}]},
        status='completed',
    )
    row = get_task_run('run_test_1')
    assert row is not None
    assert row['task_id'] == 'task_1'
    assert row['status'] == 'completed'


def test_feedback_insert():
    from app.utils.db import insert_feedback, list_feedback
    fid = insert_feedback(
        run_id='run_test_1',
        kind='section_vote',
        section_index=0,
        vote='down',
        comment='只要表格',
    )
    assert fid > 0
    rows = list_feedback('run_test_1')
    assert any(r['comment'] == '只要表格' for r in rows)


def test_playbook_rule_insert_and_get():
    from app.utils.db import insert_playbook_rule, get_playbook_rule, list_playbook_rules
    rid = insert_playbook_rule(
        rule_type='style',
        scope='user',
        target_agent='analyst',
        action='券商观点汇总章节用 Markdown 表格呈现',
        condition='当用户对券商观点章节点踩并要求表格时',
        confidence=0.8,
        evidence_run_ids=['run_test_1'],
    )
    assert rid > 0
    rule = get_playbook_rule(rid)
    assert rule is not None
    assert rule['target_agent'] == 'analyst'
    rules = list_playbook_rules(status='candidate', target_agent='analyst')
    assert any(r['id'] == rid for r in rules)
