from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import pytest

from hub.pages import user


async def test_already_settled_page_returns_to_application(monkeypatch):
    ui = MagicMock()
    monkeypatch.setattr(user, 'ui', ui)
    monkeypatch.setattr(user, 'app_layout', lambda **kwargs: nullcontext())
    monkeypatch.setattr(user, '_current_session_id', uuid4)
    monkeypatch.setattr(user, 'current_user_sub', lambda: 'player')
    monkeypatch.setattr(user, '_checkout_composition', Mock())
    monkeypatch.setattr(
        user,
        '_load_checkout_session',
        AsyncMock(
            return_value=SimpleNamespace(
                status='settled',
                amount_msat=1000,
                external_amount_msat=0,
                invoice_id=None,
                payment_request='',
                game_id='bingo',
                return_url='https://bingo.example/result?order=123',
            ),
        ),
    )
    await user.checkout_page()
    ui.navigate.to.assert_called_once_with(
        'https://bingo.example/result?order=123'
    )
    ui.timer.assert_not_called()


@pytest.mark.parametrize(
    'status',
    [
        'awaiting_payment',
        'reserved',
        'expired',
        'canceled',
        'failed',
        'settled',
    ],
)
async def test_return_requires_settled_checkout(monkeypatch, status):
    ui = Mock()
    monkeypatch.setattr(user, 'ui', ui)
    monkeypatch.setattr(user, 'current_user_sub', lambda: 'player')
    load = AsyncMock(
        return_value=SimpleNamespace(
            status=status,
            return_url='https://bingo.example/orders/123',
        )
    )
    monkeypatch.setattr(user, '_load_checkout_session', load)
    session_id = uuid4()
    user._watch_checkout_payment(session_id)
    callback = ui.timer.call_args.args[1]
    await callback()
    load.assert_awaited_once_with(session_id)
    if status == 'settled':
        ui.navigate.to.assert_called_once_with(
            'https://bingo.example/orders/123'
        )
    elif status in {'expired', 'canceled', 'failed'}:
        ui.navigate.to.assert_called_once_with(
            f'/user/checkout?session_id={session_id}'
        )
    else:
        ui.navigate.to.assert_not_called()
    if status in {'settled', 'expired', 'canceled', 'failed'}:
        ui.timer.return_value.cancel.assert_called_once()


async def test_payment_watch_stops_when_login_expires(monkeypatch):
    ui = Mock()
    load = AsyncMock()
    monkeypatch.setattr(user, 'ui', ui)
    monkeypatch.setattr(user, 'current_user_sub', lambda: None)
    monkeypatch.setattr(user, '_load_checkout_session', load)
    user._watch_checkout_payment(uuid4())
    await ui.timer.call_args.args[1]()
    load.assert_not_awaited()
    ui.navigate.to.assert_not_called()
    ui.timer.return_value.cancel.assert_called_once()
