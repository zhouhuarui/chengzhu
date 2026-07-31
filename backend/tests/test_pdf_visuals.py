"""Visual PDF extraction tests; all model calls are offline fakes."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import fitz
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.services import pdf_visuals
from app.utils.llm_client import LLMResult


class FakeVisionClient:
    def __init__(self, *, fail: bool = False, reservation_id: str = None):
        self.fail = fail
        self.reservation_id = reservation_id
        self.calls: List[Dict[str, Any]] = []

    def chat_json_result(self, messages, **kwargs):
        self.calls.append({'messages': messages, 'kwargs': kwargs})
        if self.fail:
            raise RuntimeError('boom data:image/png;base64,SECRET')
        return LLMResult(
            content='{"markdown":"### 收入图\\n- 单位：亿元","confidence":0.91,"has_visual_evidence":true}',
            provider='dashscope',
            model='qwen3-vl-plus',
            finish_reason='stop',
            usage={'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120},
            request_id='vl-request-1',
            latency_ms=12.5,
            parsed_json={
                'markdown': '### 收入图\n- 单位：亿元',
                'confidence': 0.91,
                'has_visual_evidence': True,
            },
            budget_reservation_id=self.reservation_id,
        )


def _blank_pdf(path, pages: int = 1) -> None:
    document = fitz.open()
    for _ in range(pages):
        page = document.new_page(width=320, height=240)
        page.draw_rect(fitz.Rect(30, 30, 290, 190), color=(0, 0, 0))
    document.save(path)
    document.close()


def _long_pdf_with_late_scan(path) -> None:
    document = fitz.open()
    for page_number in range(1, 13):
        page = document.new_page(width=320, height=240)
        if page_number in {1, 10}:
            page.draw_rect(fitz.Rect(30, 30, 290, 190), color=(0, 0, 0))
            continue
        for line_number in range(8):
            page.insert_text(
                (20, 25 + line_number * 20),
                f'page {page_number} financial narrative line {line_number} ' + 'x' * 42,
                fontsize=7,
            )
    document.save(path)
    document.close()


def _ocr_layered_full_page_scan(path, image_path) -> None:
    Image.new('RGB', (640, 480), color='white').save(image_path, format='PNG')
    document = fitz.open()
    page = document.new_page(width=320, height=240)
    page.insert_image(page.rect, filename=str(image_path))
    # More than 1000 extractable characters emulate an OCR text layer.  The
    # full-page raster must still make this a visual candidate.
    for line_number in range(22):
        page.insert_text(
            (5, 8 + line_number * 10),
            f'ocr financial disclosure line {line_number} ' + 'x' * 55,
            fontsize=5,
        )
    document.save(path)
    document.close()


def test_qwen_vl_uses_content_array_and_never_persists_base64(tmp_path, monkeypatch):
    pdf_path = tmp_path / 'scan.pdf'
    _blank_pdf(pdf_path)
    cache_root = tmp_path / 'uploads'
    fake = FakeVisionClient()

    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(cache_root))
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'vl-key')
    monkeypatch.setattr(Config, 'VISION_MAX_PAGES', 8)
    monkeypatch.setattr(pdf_visuals, '_new_vision_client', lambda: fake)

    result = pdf_visuals.parse_pdf_visuals(str(pdf_path))

    assert result['ok'] is True
    assert result['visual_status'] == 'completed'
    assert result['visual_incomplete'] is False
    assert result['visual_pages'][0]['file_sha256'] == result['file_sha256']
    assert result['visual_pages'][0]['confidence'] == 0.91
    assert result['visual_pages'][0]['model_metadata']['request_id'] == 'vl-request-1'

    content = fake.calls[0]['messages'][0]['content']
    assert [item['type'] for item in content] == ['image_url', 'text']
    assert content[0]['image_url']['url'].startswith('data:image/png;base64,')
    assert fake.calls[0]['kwargs']['thinking'] is False

    serialised_result = json.dumps(result, ensure_ascii=False)
    assert 'base64,' not in serialised_result
    cache_files = list((cache_root / 'pdf_cache').glob('*.json'))
    assert len(cache_files) == 1
    assert 'base64,' not in cache_files[0].read_text(encoding='utf-8')


def test_missing_vision_key_keeps_local_result_and_marks_incomplete(tmp_path, monkeypatch):
    pdf_path = tmp_path / 'scan.pdf'
    _blank_pdf(pdf_path)
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', None)
    monkeypatch.setattr(Config, 'VISION_MAX_PAGES', 8)

    result = pdf_visuals.parse_pdf_visuals(str(pdf_path))

    assert result['ok'] is True
    assert result['needs_vl'] is True
    assert result['visual_status'] == 'not_configured'
    assert result['visual_incomplete'] is True
    assert '视觉证据未完整解析' in result['vl_notes'][0]
    assert result['visual_pages'] == []


def test_standalone_image_uses_qwen_content_array_without_persisting_base64(
    tmp_path,
    monkeypatch,
):
    image_path = tmp_path / 'uploaded-chart.jpg'
    Image.new('RGB', (320, 180), color='white').save(image_path, format='JPEG')
    fake = FakeVisionClient()
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'vl-key')

    result = pdf_visuals.parse_image_visual(
        str(image_path),
        vision_client=fake,
        run_id='run-image-test',
    )

    assert result['ok'] is True
    assert result['candidate_pages'] == [1]
    assert result['visual_status'] == 'completed'
    assert result['visual_incomplete'] is False
    assert result['visual_pages'][0]['confidence'] == 0.91
    content = fake.calls[0]['messages'][0]['content']
    assert [item['type'] for item in content] == ['image_url', 'text']
    assert content[0]['image_url']['url'].startswith('data:image/png;base64,')
    assert 'base64,' not in json.dumps(result, ensure_ascii=False)


def test_standalone_image_without_key_is_retained_as_incomplete_evidence(
    tmp_path,
    monkeypatch,
):
    image_path = tmp_path / 'uploaded-chart.png'
    Image.new('RGB', (120, 80), color='white').save(image_path, format='PNG')
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', None)

    result = pdf_visuals.parse_image_visual(str(image_path))

    assert result['ok'] is True
    assert result['visual_status'] == 'not_configured'
    assert result['visual_incomplete'] is True
    assert result['visual_pages'] == []
    assert '视觉证据未完整解析' in result['vl_notes'][0]


def test_qwen_failure_is_sanitised_and_local_extraction_survives(tmp_path, monkeypatch):
    pdf_path = tmp_path / 'scan.pdf'
    _blank_pdf(pdf_path)
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'vl-key')
    monkeypatch.setattr(Config, 'VISION_MAX_PAGES', 8)

    result = pdf_visuals.parse_pdf_visuals(
        str(pdf_path),
        vision_client=FakeVisionClient(fail=True),
    )

    assert result['ok'] is True
    assert result['visual_status'] == 'failed'
    assert result['visual_incomplete'] is True
    assert '视觉证据未完整解析' in result['vl_notes'][0]
    assert '[image payload omitted]' in result['visual_errors'][0]['error']
    assert 'base64,' not in json.dumps(result, ensure_ascii=False)


def test_reserved_visual_settlement_failure_uses_local_fallback(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / 'settlement-failure.pdf'
    _blank_pdf(pdf_path)
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'vl-key')
    monkeypatch.setattr(
        'app.utils.llm_audit.record_llm_result',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError('settlement unavailable')
        ),
    )
    monkeypatch.setattr(
        'app.utils.llm_audit.record_llm_client_error',
        lambda *_args, **_kwargs: 0,
    )

    result = pdf_visuals.parse_pdf_visuals(
        str(pdf_path),
        vision_client=FakeVisionClient(reservation_id='llmres_real'),
    )

    assert result['ok'] is True
    assert result['visual_status'] == 'failed'
    assert result['visual_incomplete'] is True
    assert result['visual_pages'] == []
    assert result['visual_errors'][0]['error'] == 'OSError'


def test_visual_page_limit_is_bounded_by_config(tmp_path, monkeypatch):
    pdf_path = tmp_path / 'three-pages.pdf'
    _blank_pdf(pdf_path, pages=3)
    fake = FakeVisionClient()
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'vl-key')
    monkeypatch.setattr(Config, 'VISION_MAX_PAGES', 2)

    result = pdf_visuals.parse_pdf_visuals(
        str(pdf_path),
        max_pages=8,
        vision_client=fake,
    )

    assert result['page_count'] == 3
    assert result['analyzed_page_limit'] == 2
    assert result['candidate_pages'] == [1, 2]
    assert len(fake.calls) == 2


def test_full_document_scan_can_select_candidate_after_page_nine(tmp_path, monkeypatch):
    pdf_path = tmp_path / 'late-scan.pdf'
    _long_pdf_with_late_scan(pdf_path)
    fake = FakeVisionClient()
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'vl-key')
    monkeypatch.setattr(Config, 'VISION_MAX_PAGES', 2)

    result = pdf_visuals.parse_pdf_visuals(
        str(pdf_path),
        max_pages=2,
        vision_client=fake,
    )

    assert result['page_count'] == 12
    assert len(result['pages']) == 12
    assert result['analyzed_page_limit'] == 2
    assert result['candidate_pages'] == [1, 10]
    assert [page['page_number'] for page in result['visual_pages']] == [1, 10]
    assert len(fake.calls) == 2


def test_full_page_scan_with_long_ocr_layer_is_still_visual_candidate(
    tmp_path,
    monkeypatch,
):
    pdf_path = tmp_path / 'ocr-layered-scan.pdf'
    _ocr_layered_full_page_scan(pdf_path, tmp_path / 'page.png')
    fake = FakeVisionClient()
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'vl-key')
    monkeypatch.setattr(Config, 'VISION_MAX_PAGES', 1)

    result = pdf_visuals.parse_pdf_visuals(
        str(pdf_path),
        max_pages=1,
        vision_client=fake,
    )

    assert result['pages'][0]['text_chars'] > 1000
    assert result['pages'][0]['image_area_ratio'] >= 0.9
    assert 'large_image_region' in result['pages'][0]['candidate_reasons']
    assert result['candidate_pages'] == [1]
    assert len(fake.calls) == 1


def test_visual_cache_identity_includes_effective_page_limit(tmp_path, monkeypatch):
    pdf_path = tmp_path / 'cached-three-pages.pdf'
    _blank_pdf(pdf_path, pages=3)
    fake = FakeVisionClient()
    cache_root = tmp_path / 'uploads'
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(cache_root))
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'vl-key')
    monkeypatch.setattr(Config, 'VISION_MAX_PAGES', 8)
    monkeypatch.setattr(pdf_visuals, '_new_vision_client', lambda: fake)

    first = pdf_visuals.parse_pdf_visuals(str(pdf_path), max_pages=3)
    second = pdf_visuals.parse_pdf_visuals(str(pdf_path), max_pages=1)

    assert first['candidate_pages'] == [1, 2, 3]
    assert second['candidate_pages'] == [1]
    assert [page['page_number'] for page in second['visual_pages']] == [1]
    assert len(fake.calls) == 4
    assert len(list((cache_root / 'pdf_cache').glob('*.json'))) == 2


def test_owned_vision_client_reserves_against_run_budget(monkeypatch):
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', 'offline-key')

    client = pdf_visuals._new_vision_client(run_id='run-vision-budget')

    assert client.budget_run_id == 'run-vision-budget'
