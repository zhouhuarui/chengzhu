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

    CORS(app, resources={r'/api/*': {'origins': '*'}})

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
        return {
            'status': 'ok',
            'service': '成竹 Foresketch Backend',
            'product': 'chengzhu',
        }

    if should_log:
        logger.info('成竹 Foresketch Backend 启动完成')

    return app
