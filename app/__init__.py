import logging

from flask import Flask, jsonify

from app.config import Config
from app.db import check_db, init_db


def create_app():
    logging.basicConfig(level=logging.INFO)

    app = Flask(__name__)
    app.extensions["config"] = Config()
    init_db(app.extensions["config"].DATABASE_URL)

    @app.get("/health")
    def health():
        if check_db():
            return jsonify(status="ok", database="ok"), 200
        return jsonify(status="error", database="unavailable"), 503

    return app
