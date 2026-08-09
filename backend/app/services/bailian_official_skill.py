"""Checksum-verified runner for Alibaba Cloud's official visual Skill.

The upstream Skill explicitly requires callers to use its supplied
``image_understanding.py`` script.  This adapter therefore does not re-create
the API call.  It validates the fetched files, invokes that exact script in an
isolated subprocess, and exposes only a compatibility client to Chengzhu's
existing PDF candidate-page pipeline.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.llm_audit import reserve_llm_call_budget
from ..utils.llm_client import LLMResult


OFFICIAL_SKILL_COMMIT = '92bd723f7cc217b252feab574c1883fa0aa46b3c'
OFFICIAL_FILE_SHA256 = {
    'SKILL.md': '840b9faf3205b93d65c8a4b76a342c10b3c35e622c5a47986e08d89c7be5c6d8',
    'scripts/image_understanding.py': 'f424a10d07d978862da576af7d20efa5e43067e72dbb72e0d241fe56ea99dcb3',
    'scripts/api_key.py': '1cf3b28d63a29d7ceec7419ee2c5d546358d733500fd66f061ac5d55c3495106',
    'scripts/requirements.txt': 'c69290e63c1bbcf71488fe9e7933f26eb0fc17c0179e97cf72213b3fa0ae0469',
}
_DATA_IMAGE_RE = re.compile(
    r'^data:image/(?:png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=]+)$'
)


class OfficialSkillUnavailable(RuntimeError):
    """The locked Skill is absent, modified, or cannot be invoked safely."""


class OfficialSkillInvocationError(RuntimeError):
    """Safe operational error that never contains prompts or provider output."""

    def __init__(self, code: str):
        super().__init__(code)
        self.usage: Dict[str, int] = {}
        self.provider = 'dashscope'
        self.model = Config.AGENTTEAMS_BAILIAN_SKILL_MODEL
        self.request_id = None
        self.latency_ms = 0.0
        self.retry_count = 0
        self.budget_reservation_id: Optional[str] = None
        self._llm_safe_metadata = True
        self._llm_usage_complete = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_official_skill(root: Optional[str] = None) -> Path:
    """Return the verified Skill root or fail closed on source drift."""

    if Config.AGENTTEAMS_BAILIAN_SKILL_COMMIT != OFFICIAL_SKILL_COMMIT:
        raise OfficialSkillUnavailable('official_skill_commit_mismatch')
    skill_root = Path(root or Config.AGENTTEAMS_BAILIAN_SKILL_ROOT).resolve()
    for relative, expected in OFFICIAL_FILE_SHA256.items():
        candidate = (skill_root / relative).resolve()
        if skill_root not in candidate.parents or not candidate.is_file():
            raise OfficialSkillUnavailable('official_skill_file_missing')
        if candidate.is_symlink() or _sha256(candidate) != expected:
            raise OfficialSkillUnavailable('official_skill_checksum_mismatch')
    return skill_root


def _extract_request(messages: List[Dict[str, Any]]) -> tuple[bytes, str]:
    image_payload: Optional[bytes] = None
    question = ''
    for message in messages:
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        for item in message.get('content') or []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'image_url':
                image_url = item.get('image_url') or {}
                value = str(image_url.get('url') or '') if isinstance(image_url, dict) else ''
                match = _DATA_IMAGE_RE.fullmatch(value)
                if not match:
                    raise OfficialSkillInvocationError('official_skill_invalid_image')
                try:
                    image_payload = base64.b64decode(match.group(1), validate=True)
                except ValueError as error:
                    raise OfficialSkillInvocationError(
                        'official_skill_invalid_image'
                    ) from error
            elif item.get('type') == 'text':
                question = str(item.get('text') or '')
    if not image_payload or len(image_payload) > 20 * 1024 * 1024:
        raise OfficialSkillInvocationError('official_skill_invalid_image')
    if not question or len(question) > 8_000:
        raise OfficialSkillInvocationError('official_skill_invalid_question')
    return image_payload, question


def _parse_script_output(stdout: str) -> tuple[str, Dict[str, Any]]:
    marker = 'Analysis result:'
    if marker not in stdout:
        raise OfficialSkillInvocationError('official_skill_invalid_response')
    content = stdout.split(marker, 1)[1].strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.IGNORECASE)
        content = re.sub(r'\s*```\s*$', '', content)
    decoder = json.JSONDecoder()
    start = content.find('{')
    if start < 0:
        raise OfficialSkillInvocationError('official_skill_invalid_response')
    try:
        parsed, _end = decoder.raw_decode(content[start:])
    except (TypeError, ValueError) as error:
        raise OfficialSkillInvocationError(
            'official_skill_invalid_response'
        ) from error
    if not isinstance(parsed, dict):
        raise OfficialSkillInvocationError('official_skill_invalid_response')
    markdown = parsed.get('markdown')
    if not isinstance(markdown, str) or not markdown.strip():
        raise OfficialSkillInvocationError('official_skill_invalid_response')
    return content, parsed


class OfficialBailianSkillClient:
    """Adapter consumed by ``pdf_visuals`` while running the official script."""

    provider = 'dashscope'
    model = Config.AGENTTEAMS_BAILIAN_SKILL_MODEL
    max_retries = 1

    def __init__(
        self,
        *,
        run_id: Optional[str],
        root: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        deadline_epoch: Optional[float] = None,
    ):
        if not Config.VISION_LLM_API_KEY:
            raise OfficialSkillUnavailable('dashscope_key_not_configured')
        self.root = verify_official_skill(root)
        self.script = self.root / 'scripts/image_understanding.py'
        self.run_id = run_id
        self.deadline_epoch = deadline_epoch
        self.timeout_seconds = float(
            timeout_seconds or Config.AGENTTEAMS_BAILIAN_SKILL_TIMEOUT_SECONDS
        )

    @staticmethod
    def _subprocess_env(home: str, *, http_timeout_seconds: float) -> Dict[str, str]:
        allowed = {
            name: value
            for name, value in os.environ.items()
            if name in {
                'PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
                'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
                'http_proxy', 'https_proxy', 'no_proxy',
            }
        }
        allowed.update({
            'HOME': home,
            'PYTHONNOUSERSITE': '1',
            'DASHSCOPE_API_KEY': str(Config.VISION_LLM_API_KEY),
            'PYTHONPATH': str(Path(__file__).resolve().parent / 'bailian_guard'),
            'CHENGZHU_BAILIAN_MAX_TOKENS': '2048',
            'CHENGZHU_BAILIAN_HTTP_TIMEOUT_SECONDS': str(
                max(0.25, float(http_timeout_seconds))
            ),
        })
        return allowed

    def chat_json_result(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.1,
        max_tokens: Optional[int] = 2048,
        max_attempts: int = 2,
        **_kwargs: Any,
    ) -> LLMResult:
        del temperature, max_attempts
        image_payload, question = _extract_request(messages)
        reservation_id = reserve_llm_call_budget(
            self.run_id,
            provider=self.provider,
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            attempts=1 + self.max_retries,
        )
        started = time.perf_counter()
        retry_count = 0
        try:
            with tempfile.TemporaryDirectory(prefix='chengzhu-bailian-skill-') as temp:
                image_path = Path(temp) / 'page.png'
                image_path.write_bytes(image_payload)
                command = [
                    sys.executable,
                    str(self.script),
                    image_path.as_uri(),
                    question,
                ]
                completed = None
                for attempt in range(2):
                    attempt_timeout = self.timeout_seconds
                    if self.deadline_epoch is not None:
                        from ..utils.run_limits import bounded_timeout

                        attempt_timeout = bounded_timeout(
                            self.deadline_epoch,
                            self.timeout_seconds,
                            reserve_seconds=1.0,
                            minimum=0.25,
                            stage='bailian_official_skill',
                        )
                    try:
                        completed = subprocess.run(
                            command,
                            cwd=str(self.script.parent),
                            env=self._subprocess_env(
                                temp,
                                http_timeout_seconds=attempt_timeout,
                            ),
                            capture_output=True,
                            text=True,
                            timeout=attempt_timeout,
                            check=False,
                        )
                        break
                    except subprocess.TimeoutExpired:
                        retry_count = attempt + 1
                        if attempt == 1:
                            raise OfficialSkillInvocationError(
                                'official_skill_timeout'
                            ) from None
                if completed is None or completed.returncode != 0:
                    raise OfficialSkillInvocationError('official_skill_failed')
                content, parsed = _parse_script_output(completed.stdout)
        except Exception as error:
            safe_error = (
                error
                if isinstance(error, (OfficialSkillInvocationError, OfficialSkillUnavailable))
                else OfficialSkillInvocationError('official_skill_failed')
            )
            if isinstance(safe_error, OfficialSkillInvocationError):
                safe_error.latency_ms = round(
                    (time.perf_counter() - started) * 1000, 3
                )
                safe_error.retry_count = retry_count
                safe_error.budget_reservation_id = reservation_id
            raise safe_error from None

        return LLMResult(
            content=content,
            provider=self.provider,
            model=self.model,
            finish_reason='stop',
            usage={},
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            retry_count=retry_count,
            parsed_json=parsed,
            budget_reservation_id=reservation_id,
            usage_complete=False,
        )
