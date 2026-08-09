"""PDF table extraction with bounded Qwen-VL fallback for visual pages."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.llm_client import LLMClient, LLMResult


_CACHE_VERSION = 'pdf-visuals-v5'
_LOW_TEXT_CHARS = 80
_LARGE_IMAGE_AREA_RATIO = 0.35


class _LocalOnlyVisionClient:
    """Fail closed when the official remote visual Skill is unavailable."""

    provider = 'local-only'
    model = 'no-remote-vision'

    @staticmethod
    def chat_json_result(*_args, **_kwargs):
        raise RuntimeError('remote_visual_disabled_during_local_fallback')


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_path(
    path: str,
    file_hash: Optional[str] = None,
    *,
    page_limit: Optional[int] = None,
) -> str:
    """Return a cache path that invalidates on capability/config changes."""

    identity = '|'.join(
        [
            _CACHE_VERSION,
            os.path.abspath(path),
            file_hash or '',
            str(Config.VISION_LLM_PROVIDER or ''),
            str(Config.VISION_LLM_BASE_URL or ''),
            Config.VISION_LLM_MODEL,
            str(page_limit if page_limit is not None else Config.VISION_MAX_PAGES),
            'vision-enabled' if Config.VISION_LLM_API_KEY else 'vision-disabled',
        ]
    )
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    folder = os.path.join(Config.UPLOAD_FOLDER, 'pdf_cache')
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f'{key}.json')


def _write_json_atomic(path: str, value: Dict[str, Any]) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='.pdf-visual-', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _table_markdown(table: List[List[Any]]) -> Optional[str]:
    if not table:
        return None
    rows: List[str] = []
    width = max((len(row or []) for row in table), default=0)
    if width < 1:
        return None
    for row in table:
        cells = [str(cell or '').replace('\n', ' ').strip() for cell in (row or [])]
        cells.extend([''] * (width - len(cells)))
        rows.append('| ' + ' | '.join(cells) + ' |')
    separator = '| ' + ' | '.join(['---'] * width) + ' |'
    return '\n'.join([rows[0], separator] + rows[1:])


def _image_area_ratio(images: List[Dict[str, Any]], page: Any) -> float:
    """Return a conservative covered-page ratio for embedded raster images.

    OCR PDFs often contain a full-page scan plus a text layer.  Character
    density alone therefore cannot identify them.  Bounding boxes are clipped
    to the page, summed, and capped at one; this is used only for deterministic
    candidate selection and never persisted as source content.
    """

    try:
        page_width = max(0.0, float(getattr(page, 'width', 0) or 0))
        page_height = max(0.0, float(getattr(page, 'height', 0) or 0))
    except (TypeError, ValueError):
        return 0.0
    page_area = page_width * page_height
    if page_area <= 0:
        return 0.0

    covered = 0.0
    for image in images:
        if not isinstance(image, dict):
            continue
        try:
            x0 = max(0.0, min(page_width, float(image.get('x0', 0) or 0)))
            x1 = max(0.0, min(page_width, float(image.get('x1', 0) or 0)))
            if image.get('y0') is not None and image.get('y1') is not None:
                y0 = max(0.0, min(page_height, float(image.get('y0') or 0)))
                y1 = max(0.0, min(page_height, float(image.get('y1') or 0)))
            else:
                y0 = max(0.0, min(page_height, float(image.get('top', 0) or 0)))
                y1 = max(0.0, min(page_height, float(image.get('bottom', 0) or 0)))
        except (TypeError, ValueError):
            continue
        covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return round(min(1.0, covered / page_area), 6)


def _omit_image_payloads(value: str) -> str:
    return re.sub(
        r'data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+',
        '[image payload omitted]',
        value,
    )


def _safe_error(error: Exception) -> str:
    """Avoid persisting request bodies (especially Base64 image payloads)."""
    from ..utils.llm_audit import safe_error_summary
    summary = safe_error_summary(error)
    try:
        contains_image_payload = bool(
            re.search(r'data:image/[^;\s]+;base64,', str(error), re.IGNORECASE)
        )
    except Exception:
        contains_image_payload = False
    # Preserve only the fact that an image payload was removed.  No portion of
    # the exception message, request body, prompt or credential is retained.
    if contains_image_payload:
        return f'{summary} [image payload omitted]'
    return summary


def _vision_messages(page_number: int, png_bytes: bytes) -> List[Dict[str, Any]]:
    encoded = base64.b64encode(png_bytes).decode('ascii')
    prompt = (
        f'这是 PDF 第 {page_number} 页。只依据页面中可见内容，提取图表、表格、标题、'
        '坐标轴、单位、期间和脚注为结构化 Markdown；无法辨认的内容标记为“无法辨认”，'
        '不得补造数字。返回 JSON 对象，字段为 markdown（字符串）、confidence（0 到 1 数字）、'
        'has_visual_evidence（布尔值）。JSON 示例：'
        '{"markdown":"### 图表\\n- 指标：...","confidence":0.8,'
        '"has_visual_evidence":true}'
    )
    return [
        {
            'role': 'user',
            'content': [
                {
                    'type': 'image_url',
                    'image_url': {'url': f'data:image/png;base64,{encoded}'},
                },
                {'type': 'text', 'text': prompt},
            ],
        }
    ]


def _coerce_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, number)), 4)


def _analyse_visual_page(
    client: Any,
    *,
    page_number: int,
    png_bytes: bytes,
    file_hash: str,
    run_id: Optional[str] = None,
    deadline_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    messages = _vision_messages(page_number, png_bytes)
    metadata: Dict[str, Any] = {
        'provider': Config.VISION_LLM_PROVIDER,
        'model': Config.VISION_LLM_MODEL,
    }
    if deadline_epoch is not None:
        from ..utils.run_limits import ensure_time_remaining
        ensure_time_remaining(deadline_epoch, stage='vision_parser')
    try:
        from ..utils.llm_audit import ensure_llm_call_budget
        ensure_llm_call_budget(
            run_id,
            provider=str(getattr(client, 'provider', None) or Config.VISION_LLM_PROVIDER),
            model=str(getattr(client, 'model', None) or Config.VISION_LLM_MODEL),
            messages=messages,
            max_tokens=2048,
            attempts=2 * (1 + int(getattr(client, 'max_retries', 0) or 0)),
        )

        if hasattr(client, 'chat_json_result'):
            result: LLMResult = client.chat_json_result(
                messages,
                temperature=0.1,
                max_tokens=2048,
                max_attempts=2,
                thinking=False,
            )
            try:
                from ..utils.llm_audit import record_llm_result
                record_llm_result(run_id, 'vision_parser', result)
            except Exception:
                # A held reservation makes this a cost-ledger failure; let the
                # existing page-level visual fallback mark parsing incomplete.
                if getattr(result, 'budget_reservation_id', None):
                    raise
            data = result.parsed_json or {}
            metadata = result.to_metadata()
        else:
            # Small compatibility seam for injected/offline test clients.
            data = client.chat_json(
                messages,
                temperature=0.1,
                max_tokens=2048,
                max_attempts=2,
                thinking=False,
            )

        markdown = data.get('markdown') if isinstance(data, dict) else None
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError('视觉模型未返回有效 markdown')
    except Exception as error:
        from ..utils.llm_audit import record_llm_client_error
        record_llm_client_error(run_id, 'vision_parser', client, error)
        raise

    # Do not retain messages or image data in either the result or metadata.
    return {
        'page_number': page_number,
        'file_sha256': file_hash,
        'markdown': _omit_image_payloads(markdown.strip()),
        'confidence': _coerce_confidence(data.get('confidence')),
        'has_visual_evidence': bool(data.get('has_visual_evidence', True)),
        'model_metadata': metadata,
    }


def _new_vision_client(
    deadline_epoch: Optional[float] = None,
    run_id: Optional[str] = None,
) -> LLMClient:
    read_timeout = min(Config.LLM_READ_TIMEOUT_SECONDS, 10.0)
    if deadline_epoch is not None:
        from ..utils.run_limits import bounded_timeout
        read_timeout = bounded_timeout(
            deadline_epoch,
            read_timeout,
            stage='vision_client',
        )
    return LLMClient(
        api_key=Config.VISION_LLM_API_KEY,
        base_url=Config.VISION_LLM_BASE_URL,
        model=Config.VISION_LLM_MODEL,
        provider=Config.VISION_LLM_PROVIDER,
        read_timeout=read_timeout,
        max_retries=Config.LLM_MAX_RETRIES,
        deadline_epoch=deadline_epoch,
        deadline_reserve_seconds=3,
        budget_run_id=run_id,
    )


def _image_file_as_png(path: str) -> tuple[bytes, Dict[str, Any]]:
    """Decode an uploaded image and return bounded PNG bytes in memory only."""

    from PIL import Image, ImageOps

    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        original_width, original_height = image.size
        # Bound request size while preserving the full image.  This does not
        # create a local derivative and therefore cannot leak a Base64 payload
        # or stale visual evidence into task storage.
        image.thumbnail((2400, 2400))
        if image.mode not in {'RGB', 'RGBA'}:
            image = image.convert('RGB')
        output = io.BytesIO()
        image.save(output, format='PNG', optimize=True)
        return output.getvalue(), {
            'original_width': int(original_width),
            'original_height': int(original_height),
            'request_width': int(image.width),
            'request_height': int(image.height),
        }


def parse_image_visual(
    path: str,
    *,
    vision_client: Optional[Any] = None,
    run_id: Optional[str] = None,
    deadline_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    """Parse one standalone image through the dedicated Qwen-VL capability.

    Unlike a PDF, a standalone image has no traditional text fallback.  It is
    still published as an evidence card when vision is unavailable, with an
    explicit incomplete marker instead of being silently ignored.
    """

    try:
        file_hash = _file_sha256(path)
    except Exception as error:
        return {
            'ok': False,
            'error': _safe_error(error),
            'file_sha256': '',
            'needs_vl': True,
            'visual_status': 'failed',
            'visual_incomplete': True,
            'visual_pages': [],
            'vl_notes': ['视觉证据未完整解析：图片文件无法读取。'],
            'markdown': '',
        }

    base = {
        'ok': True,
        'file_sha256': file_hash,
        'page_count': 1,
        'analyzed_page_limit': 1,
        'candidate_pages': [1],
        'needs_vl': True,
        'tables_md': [],
    }
    if vision_client is None and not Config.VISION_LLM_API_KEY:
        return {
            **base,
            'pages': [{
                'page_number': 1,
                'vision_candidate': True,
                'candidate_reasons': ['standalone_image'],
            }],
            'visual_status': 'not_configured',
            'visual_incomplete': True,
            'visual_pages': [],
            'visual_errors': [],
            'vl_notes': ['视觉证据未完整解析：未配置 VISION_LLM_API_KEY。'],
            'markdown': '',
        }

    client = vision_client
    try:
        if deadline_epoch is not None:
            from ..utils.run_limits import ensure_time_remaining
            ensure_time_remaining(
                deadline_epoch,
                reserve_seconds=2,
                stage='standalone_image_parser',
            )
        png_bytes, image_info = _image_file_as_png(path)
        if client is None:
            if deadline_epoch is None and run_id is None:
                client = _new_vision_client()
            else:
                client = _new_vision_client(deadline_epoch, run_id=run_id)
        visual = _analyse_visual_page(
            client,
            page_number=1,
            png_bytes=png_bytes,
            file_hash=file_hash,
            run_id=run_id,
            deadline_epoch=deadline_epoch,
        )
        return {
            **base,
            'pages': [{
                'page_number': 1,
                **image_info,
                'vision_candidate': True,
                'candidate_reasons': ['standalone_image'],
            }],
            'visual_status': 'completed',
            'visual_incomplete': False,
            'visual_pages': [visual],
            'visual_errors': [],
            'vl_notes': [],
            'markdown': visual.get('markdown') or '',
        }
    except Exception as error:
        return {
            **base,
            'pages': [{
                'page_number': 1,
                'vision_candidate': True,
                'candidate_reasons': ['standalone_image'],
            }],
            'visual_status': 'failed',
            'visual_incomplete': True,
            'visual_pages': [],
            'visual_errors': [{'page_number': 1, 'error': _safe_error(error)}],
            'vl_notes': ['视觉证据未完整解析：Qwen-VL 图片解析失败。'],
            'markdown': '',
        }


def parse_pdf_visuals(
    path: str,
    max_pages: int = 8,
    *,
    vision_client: Optional[Any] = None,
    run_id: Optional[str] = None,
    deadline_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    """Extract local PDF tables and visually inspect candidate pages.

    Text/table extraction always runs first. Pages with little text or a large
    image region are rendered to PNG and sent to Qwen-VL only when a dedicated
    vision key is configured. Image Base64 exists only in request memory.
    """

    if max_pages < 1:
        raise ValueError('max_pages must be at least 1')
    page_limit = min(max_pages, Config.VISION_MAX_PAGES)

    try:
        file_hash = _file_sha256(path)
    except Exception as error:
        return {
            'ok': False,
            'error': _safe_error(error),
            'tables_md': [],
            'needs_vl': False,
            'visual_incomplete': False,
        }

    use_cache = vision_client is None
    cache = _cache_path(path, file_hash, page_limit=page_limit)
    if use_cache and os.path.isfile(cache):
        try:
            with open(cache, 'r', encoding='utf-8') as cached:
                cached_result = json.load(cached)
            # The limit is a privacy/cost boundary, not merely a presentation
            # preference. Never trust a stale or manually copied cache entry
            # that contains more visual pages than this invocation permits.
            if (
                int(cached_result.get('analyzed_page_limit', -1)) == page_limit
                and len(cached_result.get('candidate_pages') or []) <= page_limit
                and len(cached_result.get('visual_pages') or []) <= page_limit
            ):
                return cached_result
        except (OSError, ValueError, TypeError):
            # A partial/corrupt cache is ignored and replaced after extraction.
            pass

    tables_md: List[str] = []
    page_records: List[Dict[str, Any]] = []
    page_count = 0
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            page_count = len(pdf.pages)
            # Local extraction must cover the complete document.  ``page_limit``
            # only caps the number of ranked candidates sent to Qwen-VL; using
            # it here would permanently hide tables and scanned/chart pages
            # appearing later in a long filing.
            for index, page in enumerate(pdf.pages):
                text = page.extract_text() or ''
                page_tables: List[str] = []
                for table in page.extract_tables() or []:
                    markdown = _table_markdown(table)
                    if markdown:
                        page_tables.append(markdown)
                        tables_md.append(markdown)

                images = getattr(page, 'images', None) or []
                image_area_ratio = _image_area_ratio(images, page)
                vector_count = sum(
                    len(getattr(page, name, None) or [])
                    for name in ('curves', 'lines', 'rects')
                )
                text_chars = len(text.strip())
                # Logos are common, so an image alone is insufficient. A page
                # becomes a candidate when text is sparse, or when image-heavy
                # or chart-like content coexists with limited extractable text.
                chart_like = vector_count >= 3
                large_image_region = image_area_ratio >= _LARGE_IMAGE_AREA_RATIO
                candidate = text_chars < _LOW_TEXT_CHARS or (
                    (len(images) > 0 or chart_like) and text_chars < 1000
                ) or large_image_region
                reasons: List[str] = []
                if text_chars < _LOW_TEXT_CHARS:
                    reasons.append('low_text_density')
                if (len(images) > 0 or chart_like) and text_chars < 1000:
                    reasons.append('image_or_chart')
                if large_image_region:
                    reasons.append('large_image_region')
                # Rank obvious scan/chart pages before merely sparse pages.
                # The score is persisted for explainability but contains only
                # deterministic local-PDF metadata.
                candidate_score = 0
                if text_chars < _LOW_TEXT_CHARS:
                    candidate_score += 10_000 + (_LOW_TEXT_CHARS - text_chars) * 10
                if len(images) > 0 and text_chars < 1000:
                    candidate_score += 8_000 + min(len(images), 20) * 100
                if large_image_region:
                    candidate_score += 12_000 + int(image_area_ratio * 5_000)
                if chart_like and text_chars < 1000:
                    candidate_score += 5_000 + min(vector_count, 100) * 10
                page_records.append(
                    {
                        'page_number': index + 1,
                        'text_chars': text_chars,
                        'image_count': len(images),
                        'image_area_ratio': image_area_ratio,
                        'vector_count': vector_count,
                        'table_count': len(page_tables),
                        'vision_candidate': candidate,
                        'candidate_reasons': reasons,
                        'candidate_score': candidate_score,
                    }
                )
    except Exception as error:
        return {
            'ok': False,
            'error': _safe_error(error),
            'file_sha256': file_hash,
            'tables_md': tables_md,
            'needs_vl': False,
            'visual_incomplete': False,
        }

    ranked_candidates = sorted(
        (page for page in page_records if page['vision_candidate']),
        key=lambda page: (-page['candidate_score'], page['page_number']),
    )
    candidate_pages = [
        page['page_number'] for page in ranked_candidates[:page_limit]
    ]
    needs_vl = bool(candidate_pages)
    visual_pages: List[Dict[str, Any]] = []
    visual_errors: List[Dict[str, Any]] = []
    notes: List[str] = []

    if needs_vl and vision_client is None and not Config.VISION_LLM_API_KEY:
        visual_status = 'not_configured'
        visual_incomplete = True
        notes.append('视觉证据未完整解析：未配置 VISION_LLM_API_KEY。')
    elif needs_vl:
        visual_status = 'completed'
        client = vision_client
        try:
            if client is None:
                if deadline_epoch is None and run_id is None:
                    client = _new_vision_client()
                else:
                    client = _new_vision_client(
                        deadline_epoch,
                        run_id=run_id,
                    )
            import fitz

            with fitz.open(path) as document:
                for page_number in candidate_pages:
                    try:
                        if deadline_epoch is not None:
                            from ..utils.run_limits import ensure_time_remaining
                            ensure_time_remaining(
                                deadline_epoch,
                                reserve_seconds=2,
                                stage='vision_page',
                            )
                        page = document.load_page(page_number - 1)
                        pixmap = page.get_pixmap(
                            matrix=fitz.Matrix(1.5, 1.5),
                            alpha=False,
                        )
                        png_bytes = pixmap.tobytes('png')
                        visual_pages.append(
                            _analyse_visual_page(
                                client,
                                page_number=page_number,
                                png_bytes=png_bytes,
                                file_hash=file_hash,
                                run_id=run_id,
                                deadline_epoch=deadline_epoch,
                            )
                        )
                    except Exception as error:
                        visual_errors.append(
                            {
                                'page_number': page_number,
                                'error': _safe_error(error),
                            }
                        )
        except Exception as error:
            if client is None:
                from ..utils.llm_audit import record_llm_client_error
                record_llm_client_error(run_id, 'vision_parser', client, error)
            visual_errors.append({'page_number': None, 'error': _safe_error(error)})

        visual_incomplete = len(visual_pages) != len(candidate_pages)
        if visual_incomplete:
            visual_status = 'partial' if visual_pages else 'failed'
            notes.append('视觉证据未完整解析：Qwen-VL 调用失败或返回无效结果。')
    else:
        visual_status = 'not_needed'
        visual_incomplete = False

    markdown_parts = list(tables_md)
    for visual in visual_pages:
        markdown_parts.append(
            f"### 第 {visual['page_number']} 页视觉解析\n\n{visual['markdown']}"
        )

    result = {
        'ok': True,
        'path': path,
        'file_sha256': file_hash,
        'page_count': page_count,
        'analyzed_page_limit': page_limit,
        'pages': page_records,
        'tables_md': tables_md,
        'needs_vl': needs_vl,
        'candidate_pages': candidate_pages,
        'visual_status': visual_status,
        'visual_incomplete': visual_incomplete,
        'visual_pages': visual_pages,
        'visual_errors': visual_errors,
        # Keep the original field name for current consumers.
        'vl_notes': notes,
        'markdown': '\n\n'.join(markdown_parts) if markdown_parts else '（未提取到表格或视觉内容）',
    }

    # A transient model failure should be retried on the next read. Results
    # without a configured key are safe to cache under a distinct identity.
    if use_cache and (not visual_incomplete or not Config.VISION_LLM_API_KEY):
        try:
            _write_json_atomic(cache, result)
        except OSError:
            # Cache failure must not discard successfully extracted evidence.
            pass
    return result


def parse_local_visual_fallback(
    path: str,
    *,
    max_pages: int = 8,
    run_id: Optional[str] = None,
    deadline_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    """Run deterministic local extraction without a second remote provider.

    PDFs retain locally extracted text/table/page metadata. Standalone images
    retain safe dimensions/hash metadata and an explicit incomplete marker;
    neither path can silently spend tokens or send bytes to another endpoint.
    """

    client = _LocalOnlyVisionClient()
    if os.path.splitext(path)[1].lower() == '.pdf':
        return parse_pdf_visuals(
            path,
            max_pages=max_pages,
            vision_client=client,
            run_id=run_id,
            deadline_epoch=deadline_epoch,
        )
    return parse_image_visual(
        path,
        vision_client=client,
        run_id=run_id,
        deadline_epoch=deadline_epoch,
    )


def extract_chart_blocks(markdown: str) -> List[Dict[str, Any]]:
    blocks = []
    for match in re.finditer(r'```chart\s*(\{.*?\})\s*```', markdown or '', re.DOTALL):
        try:
            blocks.append(json.loads(match.group(1)))
        except Exception:
            continue
    return blocks


def chart_to_markdown_table(chart: Dict[str, Any]) -> str:
    title = chart.get('title') or '图表'
    x = chart.get('x') or []
    series = chart.get('series') or []
    header = '| 项 | ' + ' | '.join(
        str(series_item.get('name', f's{index}'))
        for index, series_item in enumerate(series)
    ) + ' |'
    separator = '| --- | ' + ' | '.join(['---'] * len(series)) + ' |'
    rows = [header, separator]
    for index, x_value in enumerate(x):
        cells = [str(x_value)]
        for series_item in series:
            data = series_item.get('data') or []
            cells.append(str(data[index] if index < len(data) else ''))
        rows.append('| ' + ' | '.join(cells) + ' |')
    refs = ', '.join(chart.get('source_refs') or [])
    return f'**{title}**（导出降级表格；来源 {refs}）\n\n' + '\n'.join(rows)
