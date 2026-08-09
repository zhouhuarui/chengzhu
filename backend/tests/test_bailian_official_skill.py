"""Checksum, isolation and safe-failure tests for the pinned Bailian Skill."""

from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.services import bailian_official_skill as official


def _messages() -> list:
    image = base64.b64encode(b'not-a-real-png-but-valid-base64').decode('ascii')
    return [{
        'role': 'user',
        'content': [
            {
                'type': 'image_url',
                'image_url': {'url': f'data:image/png;base64,{image}'},
            },
            {'type': 'text', 'text': '只提取页面可见内容并返回 JSON。'},
        ],
    }]


def test_official_skill_verification_fails_closed_on_source_drift(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        Config, 'AGENTTEAMS_BAILIAN_SKILL_COMMIT', official.OFFICIAL_SKILL_COMMIT
    )
    (tmp_path / 'SKILL.md').write_text('modified upstream file', encoding='utf-8')
    with pytest.raises(official.OfficialSkillUnavailable) as caught:
        official.verify_official_skill(str(tmp_path))
    assert str(caught.value) == 'official_skill_checksum_mismatch'

    monkeypatch.setattr(Config, 'AGENTTEAMS_BAILIAN_SKILL_COMMIT', 'unlocked')
    with pytest.raises(official.OfficialSkillUnavailable) as caught:
        official.verify_official_skill(str(tmp_path))
    assert str(caught.value) == 'official_skill_commit_mismatch'


def test_official_skill_runner_uses_exact_script_with_isolated_environment(
    tmp_path, monkeypatch,
):
    script = tmp_path / 'scripts' / 'image_understanding.py'
    script.parent.mkdir(parents=True)
    script.write_text('# official test fixture\n', encoding='utf-8')
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'dashscope-test-secret')
    monkeypatch.setattr(official, 'verify_official_skill', lambda _root=None: tmp_path)
    monkeypatch.setenv('UNRELATED_LONG_LIVED_SECRET', 'must-not-cross-process-boundary')
    observed = {}

    def fake_run(command, **kwargs):
        observed['command'] = command
        observed['kwargs'] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=(
                'Analysis result:\n'
                '{"markdown":"### 财务图表\\n- 收入：100 亿元",'
                '"confidence":0.93,"has_visual_evidence":true}'
            ),
            stderr='',
        )

    monkeypatch.setattr(official.subprocess, 'run', fake_run)
    client = official.OfficialBailianSkillClient(
        run_id=None,
        root=str(tmp_path),
        deadline_epoch=time.time() + 4,
    )
    result = client.chat_json_result(_messages())

    assert observed['command'][0] == sys.executable
    assert observed['command'][1] == str(script)
    assert observed['command'][2].startswith('file://')
    assert result.parsed_json['markdown'].startswith('### 财务图表')
    assert result.model == 'qwen3.5-plus'
    assert result.usage_complete is False
    child_env = observed['kwargs']['env']
    assert child_env['DASHSCOPE_API_KEY'] == 'dashscope-test-secret'
    assert 'UNRELATED_LONG_LIVED_SECRET' not in child_env
    assert observed['kwargs']['capture_output'] is True
    assert observed['kwargs']['check'] is False
    assert 0.25 <= observed['kwargs']['timeout'] <= 3.1
    assert child_env['CHENGZHU_BAILIAN_MAX_TOKENS'] == '2048'
    assert child_env['PYTHONPATH'].endswith('/bailian_guard')


def test_official_skill_failure_never_echoes_provider_output_or_key(
    tmp_path, monkeypatch,
):
    script = tmp_path / 'scripts' / 'image_understanding.py'
    script.parent.mkdir(parents=True)
    script.write_text('# official test fixture\n', encoding='utf-8')
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'dashscope-test-secret')
    monkeypatch.setattr(official, 'verify_official_skill', lambda _root=None: tmp_path)
    monkeypatch.setattr(
        official.subprocess,
        'run',
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout='provider accidentally returned private page text',
            stderr='dashscope-test-secret',
        ),
    )
    client = official.OfficialBailianSkillClient(run_id=None, root=str(tmp_path))

    with pytest.raises(official.OfficialSkillInvocationError) as caught:
        client.chat_json_result(_messages())
    assert str(caught.value) == 'official_skill_failed'
    assert 'private page text' not in str(caught.value)
    assert 'dashscope-test-secret' not in str(caught.value)


def test_visual_upload_authority_requires_matching_persisted_hash(tmp_path):
    from app.services.agentteam_runtime import _visual_upload_is_authorized

    files = tmp_path / 'files'
    files.mkdir()
    (files / '.visual_authorization.json').write_text(
        '{"schema_version":1,"records":[{'
        '"file_name":"report.pdf","sha256":"' + ('a' * 64) + '",'
        '"authorized":true,'
        '"purpose":"alibaba-cloud-bailian-visual-understanding",'
        '"source":"vue-upload-consent"}]}',
        encoding='utf-8',
    )

    assert _visual_upload_is_authorized(str(tmp_path), 'report.pdf', 'a' * 64)
    assert not _visual_upload_is_authorized(str(tmp_path), 'report.pdf', 'b' * 64)
    assert not _visual_upload_is_authorized(str(tmp_path), 'other.pdf', 'a' * 64)
