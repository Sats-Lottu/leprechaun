import asyncio
import json
import sys
from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs
from testcontainers.postgres import PostgresContainer

from pls import commands
from pls.models import database
from pls.models.tables import table_registry
from pls.provider import LNbitsClient, LNbitsConfig, LNbitsWallet
from pls.schemas import Envelope, Failed, Meta, Payment

LNBITS_USERNAME = "string"
LNBITS_PASSWORD = "stringst"

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@dataclass(frozen=True, slots=True)
class LNbitsTestInstance:
    url: str
    username: str
    password: str
    access_token: str
    wallet_id: str


class FakeWallet:
    def __init__(self, result: Payment | Failed) -> None:
        self.result = result
        self.created = []

    async def create_invoice(self, invoice):
        self.created.append(invoice)
        return self.result


class FakeBroker:
    def __init__(self) -> None:
        self.published = []

    async def publish(self, body, queue):
        self.published.append((body, queue))


class FakeContext:
    def __init__(self) -> None:
        self.values = {}

    def set_global(self, key, value):
        self.values[key] = value


class FakePublisher:
    def __init__(self) -> None:
        self.messages = []

    async def publish(self, message):
        self.messages.append(message)


class SessionScopeContext:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def __aenter__(self) -> AsyncSession:
        return self.session

    async def __aexit__(self, *_args) -> bool:
        return False


@pytest.fixture
def make_envelope() -> Callable[[dict[str, Any], str], Envelope]:
    def _make_envelope(
        data: dict[str, Any],
        msg_type: str = "payment.invoice.create",
    ) -> Envelope:
        return Envelope(
            meta=Meta(
                event_id="event-1",
                type=msg_type,
                correlation_id="corr-from-envelope",
                idempotency_key="idem-1",
            ),
            data=data,
        )

    return _make_envelope


@pytest.fixture
def create_invoice_payload() -> dict[str, Any]:
    return {
        "user_id": "user-1",
        "amount_msat": 21000,
        "memo": "deposit",
        "expires_in_sec": 600,
    }


@pytest.fixture
def fake_wallet_factory() -> Callable[[Payment | Failed], FakeWallet]:
    return FakeWallet


@pytest.fixture
def fake_broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def fake_context() -> FakeContext:
    return FakeContext()


@pytest.fixture
def fake_publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def published_events(monkeypatch) -> list[Envelope]:
    events = []

    async def fake_publish_event(_broker, env):
        events.append(env)

    monkeypatch.setattr(commands, "publish_event", fake_publish_event)
    return events


@pytest.fixture
def wallet_response_payload() -> dict[str, Any]:
    return {
        "wallet_balance": 20000,
        "payment": {
            "checking_id": "chk-1",
            "payment_hash": "hash-1",
            "amount": 1000,
            "fee": 0,
            "status": "success",
            "time": "2026-04-18T15:00:00+00:00",
            "created_at": "2026-04-18T15:00:00+00:00",
            "updated_at": "2026-04-18T15:00:01+00:00",
        },
    }


@pytest.fixture
def wallet_response_raw(wallet_response_payload) -> str:
    return json.dumps(wallet_response_payload)


@pytest.fixture(scope="session")
def engine():
    with PostgresContainer("postgres:16", driver="psycopg") as postgres:
        _engine = create_async_engine(postgres.get_connection_url())
        yield _engine


@pytest_asyncio.fixture
async def session(engine, monkeypatch):
    async with engine.begin() as conn:
        await conn.run_sync(table_registry.metadata.create_all)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        monkeypatch.setattr(
            database,
            "async_session_factory",
            lambda: SessionScopeContext(session),
        )
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(table_registry.metadata.drop_all)


@pytest.fixture(scope="session")
def lnbits() -> Generator[LNbitsTestInstance, None, None]:
    with (
        DockerContainer("lnbits/lnbits")
        .with_exposed_ports(5000)
        .with_env("LNBITS_BACKEND_WALLET_CLASS", "FakeWallet")
        .with_env("LNBITS_DATA_FOLDER", "/tmp") as container
    ):
        wait_for_logs(container, "Uvicorn running")
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5000)
        url = f"http://{host}:{port}"

        with httpx.Client(timeout=20.0) as http:
            response = http.put(
                f"{url}/api/v1/auth/first_install",
                json={
                    "username": LNBITS_USERNAME,
                    "password": LNBITS_PASSWORD,
                    "password_repeat": LNBITS_PASSWORD,
                },
            )
            assert response.status_code in {
                HTTPStatus.OK,
                HTTPStatus.CREATED,
            }, response.text

            access_token = response.json()["access_token"]
            headers = {
                "accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
            account_response = http.get(f"{url}/api/v1/auth", headers=headers)
            assert account_response.status_code == HTTPStatus.OK, (
                account_response.text
            )

            wallets = account_response.json()["wallets"]
            assert wallets
            wallet_id = wallets[0]["id"]

            balance_response = http.put(
                f"{url}/users/api/v1/balance",
                headers=headers,
                json={"id": wallet_id, "amount": 900000},
            )
            assert balance_response.status_code == HTTPStatus.OK, (
                balance_response.text
            )

        yield LNbitsTestInstance(
            url=url,
            username=LNBITS_USERNAME,
            password=LNBITS_PASSWORD,
            access_token=access_token,
            wallet_id=wallet_id,
        )


@pytest_asyncio.fixture
async def wallet(
    lnbits: LNbitsTestInstance,
) -> AsyncGenerator[LNbitsWallet, None]:
    lnbits_client = LNbitsClient(
        LNbitsConfig(
            base_url=lnbits.url,
            username=lnbits.username,
            password=lnbits.password,
        )
    )
    await lnbits_client.start()
    try:
        yield lnbits_client.wallet()
    finally:
        await lnbits_client.stop()
