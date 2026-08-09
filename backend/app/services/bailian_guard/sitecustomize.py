"""Process-local OpenAI guard for the checksum-locked Alibaba Skill.

Python imports ``sitecustomize`` before the upstream script.  The script file
itself remains byte-for-byte identical to Alibaba's pinned commit; this small
server-side boundary only disables SDK retries and caps completion tokens so a
single visual page cannot silently exceed Chengzhu's run budget.
"""

from __future__ import annotations

import os

import openai


if not getattr(openai, '_chengzhu_bailian_guard_installed', False):
    _OriginalOpenAI = openai.OpenAI

    class _CompletionsProxy:
        def __init__(self, completions):
            self._completions = completions

        def create(self, *args, **kwargs):
            maximum = max(1, int(os.environ.get('CHENGZHU_BAILIAN_MAX_TOKENS', '2048')))
            requested = kwargs.get('max_tokens')
            kwargs['max_tokens'] = (
                min(maximum, max(1, int(requested)))
                if requested is not None else maximum
            )
            return self._completions.create(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._completions, name)

    class _ChatProxy:
        def __init__(self, chat):
            self._chat = chat
            self.completions = _CompletionsProxy(chat.completions)

        def __getattr__(self, name):
            return getattr(self._chat, name)

    class _GuardedOpenAI:
        def __init__(self, *args, **kwargs):
            kwargs['max_retries'] = 0
            guard_timeout = max(
                0.25,
                float(os.environ.get('CHENGZHU_BAILIAN_HTTP_TIMEOUT_SECONDS', '60')),
            )
            configured = kwargs.get('timeout')
            if isinstance(configured, (int, float)):
                guard_timeout = min(guard_timeout, float(configured))
            kwargs['timeout'] = guard_timeout
            self._client = _OriginalOpenAI(*args, **kwargs)
            self.chat = _ChatProxy(self._client.chat)

        def __getattr__(self, name):
            return getattr(self._client, name)

    openai.OpenAI = _GuardedOpenAI
    openai._chengzhu_bailian_guard_installed = True

