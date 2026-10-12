import logging

from flask import Flask, jsonify

from app.config import Config, ConfigError
from app.db import check_db, init_db
from app.trendyol_client import TrendyolClient

logger = logging.getLogger(__name__)


def create_app(config=None, trendyol_client=None):
    """App factory. `config`/`trendyol_client` can be injected (tests); defaults come from env."""
    logging.basicConfig(level=logging.INFO)

    app = Flask(__name__)
    config = config or Config()
    app.extensions["config"] = config
    init_db(config.DATABASE_URL)

    if trendyol_client is None:
        try:
            trendyol_client = TrendyolClient.from_config(config)
        except ConfigError as exc:
            logger.warning("Trendyol client disabled: %s", exc)
    app.extensions["trendyol_client"] = trendyol_client

    @app.get("/health")
    def health():
        if check_db():
            return jsonify(status="ok", database="ok"), 200
        return jsonify(status="error", database="unavailable"), 503

    from app.routes import bp, register_error_handlers
    from app.stage import bp as stage_bp

    app.register_blueprint(bp)
    app.register_blueprint(stage_bp)
    register_error_handlers(app)

    from app.cli import qna_cli

    app.cli.add_command(qna_cli)
    return app
