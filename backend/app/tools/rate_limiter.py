"""按域名令牌桶限速（04§2）。"""

from __future__ import annotations

import threading
import time
from typing import Dict


class RateLimiter:
    """简单按 key 的最小间隔限速。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._last: Dict[str, float] = {}
        # 默认间隔（秒）
        self._intervals = {
            'eastmoney': 1.0,
            'cninfo': 1.0,
            'sina': 3.0,
            'cls': 1.0,
            'bocha': 0.5,
            'default': 1.0,
        }

    def set_interval(self, key: str, seconds: float) -> None:
        with self._lock:
            self._intervals[key] = seconds

    def wait(self, key: str = 'default') -> None:
        interval = self._intervals.get(key, self._intervals['default'])
        with self._lock:
            now = time.monotonic()
            last = self._last.get(key, 0.0)
            sleep_for = interval - (now - last)
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last[key] = time.monotonic()


# 全局单例
limiter = RateLimiter()
