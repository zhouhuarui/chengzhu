"""公告工具冒烟测试（需网络）。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from app.utils.db import init_db
from app.config import Config
import tempfile


@pytest.fixture(scope='module', autouse=True)
def _db():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    Config.DB_PATH = path
    Config.UPLOAD_FOLDER = os.path.dirname(path)
    from app.utils import db as dbmod
    if getattr(dbmod._local, 'conn', None):
        dbmod._local.conn.close()
        dbmod._local.conn = None
    init_db()


@pytest.mark.network
def test_fetch_announcements_maotai():
    from app.tools.announcements import fetch_announcements
    from datetime import datetime, timedelta
    end = datetime.now().strftime('%Y-%m-%d')
    start = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
    cards = fetch_announcements('600519', start, end, max_count=5)
    assert isinstance(cards, list)
    # 网络波动时允许空，但若非空则字段完备
    for c in cards:
        assert c.title
        assert c.source_type == 'announcement'
        assert c.fetch_tool == 'fetch_announcements'
        assert c.reliability == 5
