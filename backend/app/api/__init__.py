"""API 蓝图注册。"""

from flask import Flask

from .meta import meta_bp
from .memory import memory_bp
from .task import task_bp
from .report import report_bp
from .feedback import feedback_bp
from .tracking import tracking_bp
from .scenario import scenario_bp


def register_blueprints(app: Flask) -> None:
    app.register_blueprint(meta_bp, url_prefix='/api/meta')
    app.register_blueprint(memory_bp, url_prefix='/api/memory')
    app.register_blueprint(task_bp, url_prefix='/api/task')
    app.register_blueprint(report_bp, url_prefix='/api/report')
    app.register_blueprint(feedback_bp, url_prefix='/api/feedback')
    app.register_blueprint(tracking_bp, url_prefix='/api/tracking')
    app.register_blueprint(scenario_bp, url_prefix='/api/scenario')
