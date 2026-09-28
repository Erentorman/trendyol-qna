import logging
import os

from app import create_app
from app.config import ConfigError
from app.scheduler import start_scheduler
from app.trendyol_client import TrendyolClient

logger = logging.getLogger(__name__)

app = create_app()


def _start_poller():
    # With the Werkzeug reloader, code runs in a watcher parent and a serving child;
    # only the child (WERKZEUG_RUN_MAIN=true) may own the scheduler.
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    config = app.extensions["config"]
    try:
        client = TrendyolClient.from_config(config)
    except ConfigError as exc:
        logger.warning("Poller disabled: %s", exc)
        return
    start_scheduler(client, config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    _start_poller()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
