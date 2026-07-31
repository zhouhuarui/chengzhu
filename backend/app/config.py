"""配置管理：统一从项目根目录 .env 加载"""

from __future__ import annotations

import os
from dotenv import load_dotenv

project_root_env = os.path.join(os.path.dirname(__file__), '../../.env')
if os.path.exists(project_root_env):
    # 显式进程/Docker 环境优先于 .env，便于安全地临时开启网络验收。
    load_dotenv(project_root_env, override=False)
else:
    load_dotenv(override=False)


def _first_env(*names: str, default: str | None = None) -> str | None:
    """Return the first non-empty environment value.

    The legacy ``LLM_*`` names intentionally remain valid fallbacks while new
    deployments can keep text and vision credentials isolated.
    """

    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _provider_for(base_url: str, model: str, default: str) -> str:
    value = f'{base_url} {model}'.lower()
    if 'deepseek' in value:
        return 'deepseek'
    if 'dashscope' in value or model.lower().startswith('qwen'):
        return 'dashscope'
    return default


_legacy_llm_base_url = _first_env('LLM_BASE_URL')
_legacy_is_dashscope = bool(
    _legacy_llm_base_url
    and ('dashscope' in _legacy_llm_base_url.lower() or 'aliyuncs.com' in _legacy_llm_base_url.lower())
)

_text_base_url = _first_env(
    'TEXT_LLM_BASE_URL',
    'LLM_BASE_URL',
    default='https://api.deepseek.com',
) or 'https://api.deepseek.com'
_text_fast_model = _first_env(
    'TEXT_LLM_FAST_MODEL',
    'LLM_MODEL_NAME',
    default='deepseek-v4-flash',
) or 'deepseek-v4-flash'
_text_reasoning_model = _first_env(
    'TEXT_LLM_REASONING_MODEL',
    'LLM_MODEL_NAME_ANALYSIS',
    default='deepseek-v4-pro',
) or 'deepseek-v4-pro'

_vision_base_url = _first_env(
    'VISION_LLM_BASE_URL',
    # Reuse a legacy endpoint only when it is actually a DashScope endpoint;
    # never send image payloads or a Qwen model name to a DeepSeek text URL.
    default=(_legacy_llm_base_url if _legacy_is_dashscope else None)
    or 'https://dashscope.aliyuncs.com/compatible-mode/v1',
) or 'https://dashscope.aliyuncs.com/compatible-mode/v1'
_vision_model = _first_env(
    'VISION_LLM_MODEL',
    'LLM_VL_MODEL_NAME',
    default='qwen3-vl-plus',
) or 'qwen3-vl-plus'


class Config:
    """Flask 配置"""

    SECRET_KEY = os.environ.get('SECRET_KEY', 'chengzhu-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    JSON_AS_ASCII = False

    # 文本 LLM（DeepSeek OpenAI 兼容接口）。旧 LLM_* 环境变量仍是回退项。
    TEXT_LLM_API_KEY = _first_env('TEXT_LLM_API_KEY', 'LLM_API_KEY')
    TEXT_LLM_BASE_URL = _text_base_url
    TEXT_LLM_FAST_MODEL = _text_fast_model
    TEXT_LLM_REASONING_MODEL = _text_reasoning_model
    TEXT_LLM_PROVIDER = _first_env(
        'TEXT_LLM_PROVIDER',
        default=_provider_for(_text_base_url, _text_fast_model, 'deepseek'),
    ) or 'deepseek'

    # 视觉 LLM（百炼 Qwen-VL）。凭证与文本模型分离；旧 Key 仅作兼容回退。
    VISION_LLM_API_KEY = _first_env(
        'VISION_LLM_API_KEY',
        *(['LLM_API_KEY'] if _legacy_is_dashscope else []),
    )
    VISION_LLM_BASE_URL = _vision_base_url
    VISION_LLM_MODEL = _vision_model
    VISION_LLM_PROVIDER = _first_env(
        'VISION_LLM_PROVIDER',
        default=_provider_for(_vision_base_url, _vision_model, 'dashscope'),
    ) or 'dashscope'

    LLM_CONNECT_TIMEOUT_SECONDS = float(
        os.environ.get('LLM_CONNECT_TIMEOUT_SECONDS', '10')
    )
    LLM_READ_TIMEOUT_SECONDS = float(
        os.environ.get('LLM_READ_TIMEOUT_SECONDS', '180')
    )
    # Transport failures receive at most one SDK retry.
    LLM_MAX_RETRIES = int(os.environ.get('LLM_MAX_RETRIES', '1'))
    VISION_MAX_PAGES = int(os.environ.get('VISION_MAX_PAGES', '8'))
    PIPELINE_TIMEOUT_SECONDS = int(os.environ.get('PIPELINE_TIMEOUT_SECONDS', '480'))
    LLM_COST_BUDGET_CNY = float(os.environ.get('LLM_COST_BUDGET_CNY', '2'))
    DEBATE_MAX_DIMENSIONS = min(4, max(1, int(os.environ.get('DEBATE_MAX_DIMENSIONS', '4'))))
    DEBATE_MAX_CORRECTIONS = min(2, max(0, int(os.environ.get('DEBATE_MAX_CORRECTIONS', '2'))))

    # Compatibility aliases for existing services. New code should select a
    # capability-specific model above rather than sharing these values.
    LLM_API_KEY = TEXT_LLM_API_KEY
    LLM_BASE_URL = TEXT_LLM_BASE_URL
    LLM_MODEL_NAME = TEXT_LLM_FAST_MODEL
    LLM_MODEL_NAME_ANALYSIS = TEXT_LLM_REASONING_MODEL
    LLM_VL_MODEL_NAME = VISION_LLM_MODEL
    EMBEDDING_MODEL_NAME = os.environ.get('EMBEDDING_MODEL_NAME', 'text-embedding-v4')

    # Neo4j + Graphiti
    NEO4J_URI = os.environ.get('NEO4J_URI', 'bolt://localhost:7687')
    NEO4J_USER = os.environ.get('NEO4J_USER', 'neo4j')
    NEO4J_PASSWORD = os.environ.get('NEO4J_PASSWORD', 'chengzhu2026')

    # 博查搜索
    BOCHA_API_KEY = os.environ.get('BOCHA_API_KEY')

    # Datayes：默认关闭，未配置 Token 时仍可使用公开数据源演示。
    DATAYES_ENABLED = os.environ.get('DATAYES_ENABLED', 'false').lower() == 'true'
    DATAYES_PROVIDER_MODE = os.environ.get('DATAYES_PROVIDER_MODE', 'warehouse_then_api')
    DATAYES_TOKEN = os.environ.get('DATAYES_TOKEN', '')
    DATAYES_BASE_URL = os.environ.get('DATAYES_BASE_URL', 'https://api.wmcloud.com/data/v1')
    DATAYES_DATA_DIR = os.environ.get('DATAYES_DATA_DIR', '').strip()
    DATAYES_TIMEOUT_SECONDS = int(os.environ.get('DATAYES_TIMEOUT_SECONDS', '30'))
    DATAYES_PAGE_SIZE = int(os.environ.get('DATAYES_PAGE_SIZE', '5000'))
    DATAYES_MAX_RPS = float(os.environ.get('DATAYES_MAX_RPS', '1'))
    DATAYES_MAX_CONCURRENCY = int(os.environ.get('DATAYES_MAX_CONCURRENCY', '2'))
    DATAYES_LICENSE_MODE = os.environ.get('DATAYES_LICENSE_MODE', 'private_derived_only')
    DATAYES_PUBLIC_EXPORT = os.environ.get('DATAYES_PUBLIC_EXPORT', 'false').lower() == 'true'
    DATAYES_NETWORK_TESTS = os.environ.get('DATAYES_NETWORK_TESTS', 'false').lower() == 'true'

    # 私有数据模式不应向任意网页开放跨域读取。多个 origin 用逗号分隔。
    CORS_ALLOWED_ORIGINS = [
        origin.strip()
        for origin in os.environ.get(
            'CORS_ALLOWED_ORIGINS',
            'http://localhost:3000,http://127.0.0.1:3000',
        ).split(',')
        if origin.strip()
    ]

    # 可选
    TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')

    # 上传
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {
        'pdf', 'md', 'txt', 'markdown', 'png', 'jpg', 'jpeg', 'webp',
    }
    DB_PATH = os.path.join(UPLOAD_FOLDER, 'chengzhu.db')

    # 文本切块
    DEFAULT_CHUNK_SIZE = 500
    DEFAULT_CHUNK_OVERLAP = 50

    # 编排参数
    COLLECTOR_MAX_PARALLEL = int(os.environ.get('COLLECTOR_MAX_PARALLEL', '5'))
    ANALYST_MAX_TOOL_CALLS = int(os.environ.get('ANALYST_MAX_TOOL_CALLS', '6'))
    REVIEWER_MAX_ROUNDS = int(os.environ.get('REVIEWER_MAX_ROUNDS', '2'))
    TRACKING_CRON_ENABLED = os.environ.get('TRACKING_CRON_ENABLED', 'true').lower() == 'true'

    # 仿真推演
    SCENARIO_ENABLED = os.environ.get('SCENARIO_ENABLED', 'true').lower() == 'true'
    SCENARIO_AGENT_SCALE = int(os.environ.get('SCENARIO_AGENT_SCALE', '30'))
    SCENARIO_MAX_ROUNDS = int(os.environ.get('SCENARIO_MAX_ROUNDS', '10'))

    @classmethod
    def validate(cls, strict: bool = False) -> list[str]:
        """校验配置。Phase 0 默认非严格：缺 Key 只警告不阻断启动。"""
        errors: list[str] = []
        warnings: list[str] = []
        if not cls.TEXT_LLM_API_KEY:
            warnings.append('TEXT_LLM_API_KEY/LLM_API_KEY 未配置，文本模型将降级')
        if cls.LLM_MAX_RETRIES not in {0, 1}:
            errors.append('LLM_MAX_RETRIES 只能是 0 或 1')
        if cls.LLM_CONNECT_TIMEOUT_SECONDS <= 0 or cls.LLM_READ_TIMEOUT_SECONDS <= 0:
            errors.append('LLM 超时时间必须大于 0')
        if cls.VISION_MAX_PAGES < 1:
            errors.append('VISION_MAX_PAGES 必须大于等于 1')
        if cls.PIPELINE_TIMEOUT_SECONDS < 1 or cls.PIPELINE_TIMEOUT_SECONDS > 480:
            errors.append('PIPELINE_TIMEOUT_SECONDS 必须在 1 到 480 之间')
        if cls.LLM_COST_BUDGET_CNY <= 0 or cls.LLM_COST_BUDGET_CNY > 2:
            errors.append('LLM_COST_BUDGET_CNY 必须大于 0 且不超过 2')
        if not cls.BOCHA_API_KEY:
            warnings.append('BOCHA_API_KEY 未配置（Phase 1 web_search 需要）')
        if cls.DATAYES_PROVIDER_MODE not in {'warehouse_then_api', 'warehouse_only', 'api_only'}:
            errors.append('DATAYES_PROVIDER_MODE 必须是 warehouse_then_api/warehouse_only/api_only')
        if cls.DATAYES_ENABLED and not cls.DATAYES_TOKEN and not cls.DATAYES_DATA_DIR:
            warnings.append('Datayes 已启用但 Token 和数据目录均未配置，将降级到公开数据源')
        if cls.DATAYES_PUBLIC_EXPORT and cls.DATAYES_LICENSE_MODE == 'private_derived_only':
            errors.append('private_derived_only 模式禁止 DATAYES_PUBLIC_EXPORT=true')
        if (
            cls.DATAYES_ENABLED
            and cls.DATAYES_LICENSE_MODE == 'private_derived_only'
            and '*' in cls.CORS_ALLOWED_ORIGINS
        ):
            errors.append('private_derived_only 模式禁止 CORS_ALLOWED_ORIGINS=*')
        if cls.DEBUG:
            import warnings as warn_mod
            warn_mod.warn('Flask DEBUG mode is enabled. Do not use in production.', RuntimeWarning)
        if strict:
            errors.extend(warnings)
        else:
            for w in warnings:
                print(f'[config warning] {w}')
        return errors
