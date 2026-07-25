"""轻量本地化：Phase 0 先内置关键文案，后续可接 JSON 语言包。"""

from typing import Any

_MESSAGES = {
    'zh': {
        'progress': {
            'taskComplete': '任务完成',
            'taskFailed': '任务失败',
        }
    },
    'en': {
        'progress': {
            'taskComplete': 'Task completed',
            'taskFailed': 'Task failed',
        }
    },
}


def get_locale() -> str:
    return 'zh'


def set_locale(locale: str) -> None:
    pass


def t(key: str, **kwargs: Any) -> str:
    locale = get_locale()
    value: Any = _MESSAGES.get(locale, _MESSAGES['zh'])
    for part in key.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break
    if value is None:
        return key
    if kwargs and isinstance(value, str):
        try:
            return value.format(**kwargs)
        except Exception:
            return value
    return str(value)


def get_language_instruction() -> str:
    return '请使用简体中文回答。'
