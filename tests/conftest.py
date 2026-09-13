"""Keep deliberately malformed test traffic out of real game diagnostics."""
import logging

import pytest

from levelup import ws_logging


@pytest.fixture(autouse=True)
def room_creation_capacity_for_tests(monkeypatch):
    # Independent tests share one ASGI app/peer; security tests override this limit.
    from levelup import security
    monkeypatch.setattr(security, "ROOM_CREATIONS_PER_MINUTE", 10000)


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
