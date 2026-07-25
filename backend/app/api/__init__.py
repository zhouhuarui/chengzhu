"""API 蓝图注册。"""

from flask import Flask

from .meta import meta_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(meta_bp, url_prefix='/api/meta')
    # Phase 3+ 陆续注册：task / report / feedback / memory / tracking / scenario
