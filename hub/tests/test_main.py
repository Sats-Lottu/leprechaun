import pytest
from starlette.middleware.sessions import SessionMiddleware

from hub import main


class FakeBroker:
    def __init__(self) -> None:
        self.entered = False
        self.started = False
        self.exited = False

    async def __aenter__(self) -> 'FakeBroker':
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exited = True

    async def start(self) -> None:
        self.started = True


def test_app_registers_auth_routes() -> None:
    paths = {route.path for route in main.app.routes}

    assert '/auth/login' in paths
    assert '/auth/callback' in paths
    assert '/auth/logout' in paths
    assert '/docs/asyncapi' in paths
    assert '/api/checkout/sessions' in paths
    assert '/api/checkout/sessions/{session_id}/settle' in paths


def test_logo_asset_is_available() -> None:
    assert (main.static_directory / 'leprechaun-logo.png').is_file()


def test_app_installs_session_middleware() -> None:
    assert any(
        middleware.cls is SessionMiddleware
        for middleware in main.app.user_middleware
    )


@pytest.mark.asyncio
async def test_broker_lifespan_starts_broker(monkeypatch) -> None:
    broker = FakeBroker()
    monkeypatch.setattr(main, 'broker', broker)

    async with main.broker_lifespan(main.app):
        assert broker.entered
        assert broker.started
        assert not broker.exited

    assert broker.exited
