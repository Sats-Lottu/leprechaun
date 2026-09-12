import asyncio

import pytest

from pls import lnbits_ws_bridge
from pls.lnbits_ws_bridge import LNbitsWSBridge, lnbits_ws_url
from pls.schemas import Envelope, Meta


def test_lnbits_ws_url_uses_matching_ws_scheme():
    assert (
        lnbits_ws_url("https://lnbits.example/", "inkey")
        == "wss://lnbits.example/api/v1/ws/inkey"
    )
    assert (
        lnbits_ws_url("http://lnbits.example", "inkey")
        == "ws://lnbits.example/api/v1/ws/inkey"
    )
    assert (
        lnbits_ws_url("lnbits.example", "inkey")
        == "wss://lnbits.example/api/v1/ws/inkey"
    )


def test_parse_event_returns_wallet_response_for_valid_lnbits_payload(
    wallet_response_raw,
):
    event = lnbits_ws_bridge._parse_event(wallet_response_raw)

    assert event is not None
    assert event.wallet_balance == 20000  # noqa: PLR2004
    assert event.payment.checking_id == "chk-1"
    assert event.payment.amount == 1000  # noqa: PLR2004


def test_parse_event_returns_none_for_invalid_payload():
    assert lnbits_ws_bridge._parse_event('{"wallet_balance": 100}') is None


@pytest.mark.asyncio
async def test_bridge_start_creates_one_background_task_and_stop_cancels_it(
    monkeypatch,
    fake_publisher,
):
    started = asyncio.Event()

    async def fake_connect_and_consume(self):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        LNbitsWSBridge,
        "_connect_and_consume",
        fake_connect_and_consume,
    )
    bridge = LNbitsWSBridge(
        url="http://lnbits.test",
        invoice_read_key="inkey",
        publisher=fake_publisher,
    )

    await bridge.start()
    first_task = bridge._task
    await bridge.start()

    assert first_task is bridge._task
    await asyncio.wait_for(started.wait(), timeout=1)

    await bridge.stop()

    assert bridge._task is None
    assert first_task.cancelled()


@pytest.mark.asyncio
async def test_bridge_connect_and_consume_publishes_valid_wallet_events(
    monkeypatch,
    fake_publisher,
    wallet_response_raw,
):
    raw_messages = [wallet_response_raw, '{"wallet_balance": 100}']

    class FakeWebSocket:
        def __init__(self) -> None:
            self._messages = iter(raw_messages)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                raise StopAsyncIteration

    def fake_connect(*_args, **_kwargs):
        return FakeWebSocket()

    monkeypatch.setattr(lnbits_ws_bridge.websockets, "connect", fake_connect)
    bridge = LNbitsWSBridge(
        url="http://lnbits.test",
        invoice_read_key="inkey",
        publisher=fake_publisher,
    )

    await bridge._connect_and_consume()

    assert fake_publisher.messages == [
        {
            "payment": {
                "status": "success",
                "checking_id": "chk-1",
                "payment_hash": "hash-1",
                "preimage": None,
                "payment_request": None,
                "bolt11": None,
                "amount": 1000,
                "fee": 0,
                "memo": None,
                "time": "2026-04-18T15:00:00+00:00",
                "created_at": "2026-04-18T15:00:00+00:00",
                "updated_at": "2026-04-18T15:00:01+00:00",
            },
            "wallet_balance": 20000,
        }
    ]


@pytest.mark.asyncio
async def test_bridge_connect_and_consume_publishes_domain_handler_events(
    monkeypatch,
    fake_publisher,
    wallet_response_raw,
):
    raw_messages = [wallet_response_raw]

    class FakeWebSocket:
        def __init__(self) -> None:
            self._messages = iter(raw_messages)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                raise StopAsyncIteration

    async def event_handler(evt):
        return [
            Envelope(
                meta=Meta(type="payment.invoice.paid"),
                data={"invoice_id": evt.payment.checking_id},
            ),
            {"raw": evt.wallet_balance},
        ]

    def fake_connect(*_args, **_kwargs):
        return FakeWebSocket()

    monkeypatch.setattr(lnbits_ws_bridge.websockets, "connect", fake_connect)
    bridge = LNbitsWSBridge(
        url="http://lnbits.test",
        invoice_read_key="inkey",
        publisher=fake_publisher,
        event_handler=event_handler,
    )

    await bridge._connect_and_consume()

    assert fake_publisher.messages[0]["meta"]["type"] == (
        "payment.invoice.paid"
    )
    assert fake_publisher.messages[0]["data"] == {"invoice_id": "chk-1"}
    assert fake_publisher.messages[1] == {"raw": 20000}
