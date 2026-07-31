"""公告全文精读（04§3.10）— 分析阶段按需调用。"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

import httpx

from ..config import Config
from ._helpers import truncate
from .rate_limiter import limiter


def download_announcement_pdf(url: str) -> str:
    """下载 PDF 并抽取文本；>50 页取前 30 + 后 5。"""
    cache_dir = os.path.join(Config.UPLOAD_FOLDER, 'cache', 'announcements')
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.md5(url.encode('utf-8')).hexdigest()
    cache_path = os.path.join(cache_dir, f'{key}.txt')
    if os.path.exists(cache_path):
        with open(cache_path, 'r', encoding='utf-8') as f:
            return f.read()

    limiter.wait('cninfo')
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        r = client.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        pdf_bytes = r.content

    pdf_path = os.path.join(cache_dir, f'{key}.pdf')
    with open(pdf_path, 'wb') as f:
        f.write(pdf_bytes)

    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        n = len(reader.pages)
        indices = list(range(n))
        if n > 50:
            indices = list(range(30)) + list(range(max(30, n - 5), n))
        parts = []
        for i in indices:
            try:
                parts.append(reader.pages[i].extract_text() or '')
            except Exception:
                continue
        text = '\n'.join(parts)
    except Exception as e:
        text = f'[PDF 解析失败] {e}'

    with open(cache_path, 'w', encoding='utf-8') as f:
        f.write(text)
    return text


def read_announcement(
    url: str,
    question: Optional[str] = None,
    run_id: Optional[str] = None,
) -> str:
    text = download_announcement_pdf(url)
    # Phase 6：若本地缓存了 PDF 文件则补充表格抽取
    try:
        cache_dir = os.path.join(Config.UPLOAD_FOLDER, 'cache', 'announcements')
        key = hashlib.md5(url.encode('utf-8')).hexdigest()
        pdf_path = os.path.join(cache_dir, f'{key}.pdf')
        if os.path.isfile(pdf_path):
            from ..services.pdf_visuals import parse_pdf_visuals
            vis = parse_pdf_visuals(pdf_path, run_id=run_id)
            visual_markdown = str(vis.get('markdown') or '').strip()
            if visual_markdown and visual_markdown != '（未提取到表格或视觉内容）':
                text = text + '\n\n## PDF 结构化解析\n' + visual_markdown
            if vis.get('visual_incomplete'):
                text += '\n\n视觉证据未完整解析；当前仅保留传统文本/表格解析结果。'
    except Exception:
        pass
    if not question:
        return truncate(text, 3000)

    # 定向摘要：有 Key 时调 LLM，否则截断返回
    if not Config.LLM_API_KEY:
        return truncate(f'【问题】{question}\n【原文摘录】\n{text}', 1500)

    client = None
    try:
        from ..utils.llm_client import LLMClient
        client = LLMClient(
            api_key=Config.TEXT_LLM_API_KEY,
            base_url=Config.TEXT_LLM_BASE_URL,
            model=Config.TEXT_LLM_FAST_MODEL,
            provider=Config.TEXT_LLM_PROVIDER,
            budget_run_id=run_id,
        )
        result = client.chat_result(
            messages=[
                {'role': 'system', 'content': '你是投研助理。仅依据给定公告原文回答问题，不超过 1500 字，标注不确定处。'},
                {'role': 'user', 'content': f'问题：{question}\n\n公告原文（可能截断）：\n{text[:30000]}'},
            ],
            temperature=0.2,
            thinking=False,
        )
        from ..utils.llm_audit import record_llm_result
        record_llm_result(run_id, 'announcement_reader', result)
        return truncate(result.content, 1500)
    except Exception as error:
        from ..utils.llm_audit import record_llm_client_error
        record_llm_client_error(run_id, 'announcement_reader', client, error)
        return truncate(f'【问题】{question}\n【原文摘录】\n{text}', 1500)
