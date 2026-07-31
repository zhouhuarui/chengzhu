"""成竹 Foresketch Backend — Flask 应用工厂"""

import os
import warnings

warnings.filterwarnings('ignore', message='.*resource_tracker.*')

from flask import Flask, request
from flask_cors import CORS

from .config import Config
from .utils.logger import setup_logger, get_logger
from .utils.db import init_db


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # 应用工厂也必须执行安全校验，避免 gunicorn/测试 WSGI 等入口绕过
    # run.py 的启动检查，在私有授权数据模式下意外开放通配 CORS。
    validate = getattr(config_class, 'validate', None)
    if callable(validate):
        config_errors = validate(strict=False)
        if config_errors:
            raise RuntimeError('配置错误：' + '；'.join(config_errors))

    if hasattr(app, 'json') and hasattr(app.json, 'ensure_ascii'):
        app.json.ensure_ascii = False

    logger = setup_logger('chengzhu')
    is_reloader = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    debug_mode = app.config.get('DEBUG', False)
    should_log = not debug_mode or is_reloader

    if should_log:
        logger.info('=' * 50)
        logger.info('成竹 Foresketch Backend 启动中...')
        logger.info('=' * 50)

    CORS(
        app,
        resources={r'/api/*': {'origins': app.config.get('CORS_ALLOWED_ORIGINS', [])}},
    )

    # 初始化 SQLite
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    init_db()
    if should_log:
        logger.info(f'SQLite ready: {Config.DB_PATH}')

    @app.before_request
    def log_request():
        get_logger('chengzhu.request').debug(f'{request.method} {request.path}')

    @app.after_request
    def log_response(response):
        get_logger('chengzhu.request').debug(f'response {response.status_code}')
        return response

    from .api import register_blueprints
    register_blueprints(app)

    @app.route('/health')
    @app.route('/api/health')
    def health():
        graph_backend = 'local'
        neo4j_ok = False
        try:
            from .utils.neo4j_store import neo4j_available
            neo4j_ok = neo4j_available()
            if neo4j_ok:
                graph_backend = 'neo4j+local'
        except Exception:
            pass
        return {
            'status': 'ok',
            'service': '成竹 Foresketch Backend',
            'product': 'chengzhu',
            'graph_backend': graph_backend,
            'neo4j': neo4j_ok,
            'llm_configured': bool(Config.LLM_API_KEY),
            'bocha_configured': bool(Config.BOCHA_API_KEY),
        }

    # 追踪调度器（每进程一次）
    if should_log:
        try:
            from .services.tracking_service import start_scheduler
            start_scheduler(app)
            logger.info('Tracking scheduler started')
        except Exception as e:
            logger.warning(f'Tracking scheduler skipped: {e}')
        logger.info('成竹 Foresketch Backend 启动完成')

    return app
