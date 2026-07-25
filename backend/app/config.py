"""配置管理：统一从项目根目录 .env 加载"""

import os
from dotenv import load_dotenv

project_root_env = os.path.join(os.path.dirname(__file__), '../../.env')
if os.path.exists(project_root_env):
    load_dotenv(project_root_env, override=True)
else:
    load_dotenv(override=True)


class Config:
    """Flask 配置"""

    SECRET_KEY = os.environ.get('SECRET_KEY', 'chengzhu-secret-key')
    DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    JSON_AS_ASCII = False

    # LLM（阿里百炼 OpenAI 兼容）
    LLM_API_KEY = os.environ.get('LLM_API_KEY')
    LLM_BASE_URL = os.environ.get(
        'LLM_BASE_URL', 'https://dashscope.aliyuncs.com/compatible-mode/v1'
    )
    LLM_MODEL_NAME = os.environ.get('LLM_MODEL_NAME', 'qwen-plus')
    LLM_MODEL_NAME_ANALYSIS = os.environ.get('LLM_MODEL_NAME_ANALYSIS', 'qwen-max')
    LLM_VL_MODEL_NAME = os.environ.get('LLM_VL_MODEL_NAME', 'qwen-vl-plus')
    EMBEDDING_MODEL_NAME = os.environ.get('EMBEDDING_MODEL_NAME', 'text-embedding-v4')

    # Neo4j + Graphiti
    NEO4J_URI = os.environ.get('NEO4J_URI', 'bolt://localhost:7687')
    NEO4J_USER = os.environ.get('NEO4J_USER', 'neo4j')
    NEO4J_PASSWORD = os.environ.get('NEO4J_PASSWORD', 'chengzhu2026')

    # 博查搜索
    BOCHA_API_KEY = os.environ.get('BOCHA_API_KEY')

    # 可选
    TUSHARE_TOKEN = os.environ.get('TUSHARE_TOKEN', '')

    # 上传
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}
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
        if not cls.LLM_API_KEY:
            warnings.append('LLM_API_KEY 未配置（Phase 2 起需要）')
        if not cls.BOCHA_API_KEY:
            warnings.append('BOCHA_API_KEY 未配置（Phase 1 web_search 需要）')
        if cls.DEBUG:
            import warnings as warn_mod
            warn_mod.warn('Flask DEBUG mode is enabled. Do not use in production.', RuntimeWarning)
        if strict:
            errors.extend(warnings)
        else:
            for w in warnings:
                print(f'[config warning] {w}')
        return errors
