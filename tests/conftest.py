"""Keep deliberately malformed test traffic out of real game diagnostics."""
import logging

import pytest

from levelup import ws_logging


@pytest.fixture(scope="session", autouse=True)
def isolated_websocket_logs(tmp_path_factory):
    logger = logging.Logger("levelup.websocket.tests", logging.INFO)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LEVELUP_LOG_DIR", str(tmp_path_factory.mktemp("websocket-logs")))
        patch.setattr(ws_logging, "logger", logger)
        try:
            yield
        finally:
            for handler in logger.handlers:
                handler.close()
