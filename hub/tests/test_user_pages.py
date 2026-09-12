from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from hub.ledger_client import LedgerBalance, LedgerClientError
from hub.models.tables import CheckoutSession
from hub.pages import user

AMOUNT_MSAT = 25_000
SESSION_ID = UUID('00000000-0000-0000-0000-000000000123')
AVAILABLE_BALANCE_MSAT = 25_000
RESERVED_BALANCE_MSAT = 5_000
TOTAL_BALANCE_MSAT = 30_000
OPEN_INVOICE_COUNT = 2


class FakeElement:
    def __init__(self, value=None) -> None:
        self.value = value
        self.text = value

    def __enter__(self) -> 'FakeElement':
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def classes(self, value: str) -> 'FakeElement':
        self.classes_value = value
        return self

    def props(self, value: str) -> 'FakeElement':
        self.props_value = value
        return self

    def tooltip(self, value: str) -> 'FakeElement':
        self.tooltip_value = value
        return self

    def on(self, event: str, handler) -> 'FakeElement':
        self.event = event
        self.handler = handler
        return self


class FakeUi:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.buttons: list[str] = []
        self.images: list[str] = []
        self.links: list[str] = []
        self.notifications: list[dict] = []
        self.navigation: list[str] = []
        self.navigate = SimpleNamespace(to=self.navigation.append)
        self.context = SimpleNamespace(
            client=SimpleNamespace(
                request=SimpleNamespace(query_params={})
            )
        )

    def label(self, value: str) -> FakeElement:
        self.labels.append(value)
        return FakeElement()

    def button(
        self,
        label: str,
        *,
        icon: str,
        on_click=None,
    ) -> FakeElement:
        self.buttons.append(label)
        return FakeElement()

    def image(self, source: str) -> FakeElement:
        self.images.append(source)
        return FakeElement()

    def link(self, *, target: str) -> FakeElement:
        self.links.append(target)
        return FakeElement()

    def notify(self, message: str, *, type: str) -> None:
        self.notifications.append({'message': message, 'type': type})

    @staticmethod
    def card() -> FakeElement:
        return FakeElement()

    @staticmethod
    def grid(*, columns: int) -> FakeElement:
        return FakeElement()

    @staticmethod
    def row() -> FakeElement:
        return FakeElement()

    @staticmethod
    def column() -> FakeElement:
        return FakeElement()

    @staticmethod
    def number(_label: str, *, value: int, min: int) -> FakeElement:
        return FakeElement(value)

    @staticmethod
    def input(_label: str, *, value: str) -> FakeElement:
        return FakeElement(value)

    @staticmethod
    def textarea(_label: str, *, value: str) -> FakeElement:
        return FakeElement(value)


@contextmanager
def fake_app_layout(**_kwargs) -> Iterator[None]:
    yield


@pytest.mark.asyncio
async def test_wallet_page_renders_unavailable_balance_when_ledger_fails(
    monkeypatch,
) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(user, 'ui', fake_ui)
    monkeypatch.setattr(user, 'app_layout', fake_app_layout)
    monkeypatch.setattr(user, 'current_user_sub', lambda: 'user-123')
    monkeypatch.setattr(
        user,
        'current_user',
        lambda: {
            'sub': 'user-123',
            'name': 'Alice',
            'preferred_username': 'alice',
            'email': 'alice@example.com',
        },
    )
    monkeypatch.setattr(user, 'current_roles', lambda: {'admin'})

    async def fail_account_lookup(**_kwargs):
        raise LedgerClientError('ledger unavailable')

    monkeypatch.setattr(
        user,
        'get_or_create_user_ledger_account',
        fail_account_lookup,
    )

    await user.wallet_page()

    assert {'message': 'Ledger is unavailable.', 'type': 'warning'} in (
        fake_ui.notifications
    )
    assert 'Available Balance' in fake_ui.labels
    assert 'User identity' in fake_ui.labels
    assert 'Alice' in fake_ui.labels
    assert 'alice@example.com' in fake_ui.labels
    assert 'Wallet account' in fake_ui.labels
    assert 'Unavailable' in fake_ui.labels
    assert {
        'Sync Ledger Account',
        'Refresh Wallet',
        'Create Lightning Invoice',
        'Send Lightning Withdrawal',
    }.issubset(fake_ui.buttons)


@pytest.mark.asyncio
async def test_checkout_page_prompts_for_checkout_link(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(user, 'ui', fake_ui)
    monkeypatch.setattr(user, 'app_layout', fake_app_layout)
    monkeypatch.setattr(user, '_current_session_id', lambda: None)

    await user.checkout_page()

    assert fake_ui.labels == ['Open a checkout link to start payment.']


@pytest.mark.asyncio
async def test_checkout_page_renders_anonymous_checkout_session(
    session,
    monkeypatch,
) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(user, 'ui', fake_ui)
    monkeypatch.setattr(user, 'app_layout', fake_app_layout)
    monkeypatch.setattr(user, '_current_session_id', lambda: SESSION_ID)
    monkeypatch.setattr(user, 'current_user_sub', lambda: None)
    checkout_session = CheckoutSession(
        game_id='leprechaun-game',
        order_id='order-1',
        amount_msat=AMOUNT_MSAT,
        internal_amount_msat=10_000,
        external_amount_msat=15_000,
        invoice_id='invoice-1',
        status='awaiting_payment',
        payment_request='lnbc1test',
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
        checkout_url=f'https://hub.example/user/checkout?session_id={SESSION_ID}',
        expires_at=datetime(2026, 4, 20, 12, 15, tzinfo=timezone.utc),
    )
    checkout_session.id = SESSION_ID
    session.add(checkout_session)
    await session.commit()

    await user.checkout_page()

    assert 'Order Amount' in fake_ui.labels
    assert '25 sats' in fake_ui.labels
    assert 'Payment composition' in fake_ui.labels
    assert 'Internal balance: 10 sats' in fake_ui.labels
    assert 'External invoice: 15 sats' in fake_ui.labels
    assert 'lightning:lnbc1test' in fake_ui.links
    assert 'lnbc1test' in fake_ui.labels


@pytest.mark.asyncio
async def test_profile_page_renders_authenticated_user(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(user, 'ui', fake_ui)
    monkeypatch.setattr(user, 'app_layout', fake_app_layout)
    monkeypatch.setattr(user, 'current_user_sub', lambda: 'user-123')
    monkeypatch.setattr(
        user,
        'current_user',
        lambda: {
            'sub': 'user-123',
            'name': 'Alice',
            'preferred_username': 'alice',
            'email': 'alice@example.com',
        },
    )
    monkeypatch.setattr(user, 'current_roles', lambda: {'admin'})
    monkeypatch.setattr(
        user,
        'load_wallet_snapshot',
        AsyncMock(side_effect=LedgerClientError('ledger unavailable')),
    )

    await user.profile_page()

    assert 'User identity' in fake_ui.labels
    assert 'Alice' in fake_ui.labels
    assert 'user-123' in fake_ui.labels
    assert 'Detailed history' in fake_ui.labels


@pytest.mark.asyncio
async def test_request_deposit_invoice_uses_pls_contract() -> None:
    captured = {}

    async def fake_invoice_requester(**kwargs):
        captured.update(kwargs)
        return {
            'invoice_id': 'invoice-1',
            'payment_request': 'lnbc1test',
            'expires_at': '2026-04-21T12:00:00+00:00',
        }

    response = await user.request_deposit_invoice(
        user_sub='user-123',
        amount_sats=21,
        memo='top up',
        invoice_requester=fake_invoice_requester,
    )

    assert captured == {
        'user_id': 'user-123',
        'amount_msat': 21_000,
        'memo': 'top up',
    }
    assert response == {
        'invoice_id': 'invoice-1',
        'payment_request': 'lnbc1test',
        'expires_at': '2026-04-21T12:00:00+00:00',
    }


@pytest.mark.asyncio
async def test_request_deposit_invoice_rejects_invalid_amount() -> None:
    with pytest.raises(ValueError, match='amount_sats must be positive'):
        await user.request_deposit_invoice(
            user_sub='user-123',
            amount_sats=0,
        )


@pytest.mark.asyncio
async def test_withdraw_wallet_balance_posts_debit_after_pls_success(
    monkeypatch,
) -> None:
    user_account = UUID('00000000-0000-0000-0000-000000000001')
    settlement_account = UUID('00000000-0000-0000-0000-000000000002')
    transaction_id = UUID('00000000-0000-0000-0000-000000000003')
    account = SimpleNamespace(ledger_account_id=user_account)
    monkeypatch.setattr(
        user,
        'get_or_create_user_ledger_account',
        AsyncMock(return_value=account),
    )
    monkeypatch.setattr(
        user,
        'get_settings',
        lambda: SimpleNamespace(
            LIGHTNING_SETTLEMENT_ACCOUNT_ID=settlement_account
        ),
    )
    ledger = SimpleNamespace(
        get_balance=AsyncMock(
            return_value=LedgerBalance(
                balance_msat=100_000,
                reserved_balance_msat=0,
                available_balance_msat=100_000,
            )
        ),
        create_transaction=AsyncMock(
            return_value=SimpleNamespace(transaction_id=transaction_id)
        ),
        post_transaction=AsyncMock(
            return_value=SimpleNamespace(transaction_id=transaction_id)
        ),
    )

    async def fake_payout_requester(**kwargs):
        assert kwargs == {
            'user_id': 'user-123',
            'payment_request': 'lnbc1withdraw',
            'amount_msat': 21_000,
        }
        return {'checking_id': 'chk-1', 'payment_hash': 'hash-1'}

    result = await user.withdraw_wallet_balance(
        user_sub='user-123',
        amount_sats=21,
        payment_request=' lnbc1withdraw ',
        ledger=ledger,
        session=SimpleNamespace(),
        payout_requester=fake_payout_requester,
    )

    assert result is not None
    assert result.transaction_id == transaction_id
    payload = ledger.create_transaction.await_args.args[0]
    assert payload.entries[0].account_id == user_account
    assert payload.entries[0].entry_type == 'debit'
    assert payload.entries[1].account_id == settlement_account
    assert payload.entries[1].entry_type == 'credit'


@pytest.mark.asyncio
async def test_withdraw_wallet_balance_requires_configured_settlement_account(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        user,
        'get_settings',
        lambda: SimpleNamespace(LIGHTNING_SETTLEMENT_ACCOUNT_ID=None),
    )

    with pytest.raises(user.WalletWithdrawalError):
        await user.withdraw_wallet_balance(
            user_sub='user-123',
            amount_sats=21,
            payment_request='lnbc1withdraw',
            ledger=SimpleNamespace(),
            session=SimpleNamespace(),
        )


def test_current_user_identity_prefers_name_and_roles(monkeypatch) -> None:
    monkeypatch.setattr(
        user,
        'current_user',
        lambda: {
            'sub': 'user-123',
            'name': 'Alice',
            'preferred_username': 'alice',
            'email': 'alice@example.com',
        },
    )
    monkeypatch.setattr(user, 'current_user_sub', lambda: 'user-123')
    monkeypatch.setattr(user, 'current_roles', lambda: {'admin', 'support'})

    identity = user._current_user_identity()  # noqa: SLF001

    assert identity.display_name == 'Alice'
    assert identity.preferred_username == 'alice'
    assert identity.email == 'alice@example.com'
    assert identity.roles == ('admin', 'support')


@pytest.mark.asyncio
async def test_load_wallet_snapshot_aggregates_ledger_and_checkout_data(
    monkeypatch,
) -> None:
    session = SimpleNamespace()
    account = SimpleNamespace(
        ledger_account_id=UUID('00000000-0000-0000-0000-000000000001')
    )
    checkout = SimpleNamespace(
        game_id='leprechaun-game',
        order_id='order-1',
        status='awaiting_payment',
        amount_msat=25_000,
        internal_amount_msat=5_000,
        external_amount_msat=20_000,
    )

    async def fake_scalar(statement):
        compiled = str(statement)
        if 'count(checkout_sessions.id)' in compiled:
            return 2
        raise AssertionError(compiled)

    async def fake_scalars(statement):
        compiled = str(statement)
        if 'FROM checkout_sessions' in compiled:
            return [checkout]
        raise AssertionError(compiled)

    session.scalar = fake_scalar
    session.scalars = fake_scalars
    monkeypatch.setattr(
        user,
        'get_or_create_user_ledger_account',
        AsyncMock(return_value=account),
    )
    ledger = SimpleNamespace(
        get_balance=AsyncMock(
            return_value=LedgerBalance(
                balance_msat=TOTAL_BALANCE_MSAT,
                reserved_balance_msat=RESERVED_BALANCE_MSAT,
                available_balance_msat=AVAILABLE_BALANCE_MSAT,
            )
        ),
        list_holds=AsyncMock(
            return_value=[
                SimpleNamespace(
                    hold_id=UUID('00000000-0000-0000-0000-000000000010'),
                    account_id=account.ledger_account_id,
                    amount_msat=RESERVED_BALANCE_MSAT,
                    status='active',
                    reason='checkout',
                    reference_type='checkout_session',
                    expires_at=None,
                )
            ]
        ),
        get_statement=AsyncMock(
            return_value=[
                SimpleNamespace(
                    account_id=account.ledger_account_id,
                    entry_type='credit',
                    amount_msat=TOTAL_BALANCE_MSAT,
                    description='deposit',
                    reference_type='payment.invoice.paid',
                )
            ]
        ),
    )

    snapshot = await user.load_wallet_snapshot(
        user_sub='user-123',
        session=session,
        ledger=ledger,
    )

    assert snapshot.account_id == account.ledger_account_id
    assert snapshot.available_balance_msat == AVAILABLE_BALANCE_MSAT
    assert snapshot.reserved_balance_msat == RESERVED_BALANCE_MSAT
    assert snapshot.total_balance_msat == TOTAL_BALANCE_MSAT
    assert snapshot.open_invoice_count == OPEN_INVOICE_COUNT
    assert snapshot.active_holds[0].amount_msat == RESERVED_BALANCE_MSAT
    assert snapshot.statement_entries[0].entry_type == 'credit'
    assert snapshot.recent_checkouts == (checkout,)


def test_current_session_id_reads_query_param(monkeypatch) -> None:
    fake_ui = FakeUi()
    fake_ui.context.client.request.query_params['session_id'] = str(SESSION_ID)
    monkeypatch.setattr(user, 'ui', fake_ui)

    assert user._current_session_id() == SESSION_ID  # noqa: SLF001


@pytest.mark.parametrize('raw_session_id', ['', 'not-a-uuid'])
def test_current_session_id_rejects_missing_or_invalid_query_param(
    monkeypatch,
    raw_session_id: str,
) -> None:
    fake_ui = FakeUi()
    fake_ui.context.client.request.query_params['session_id'] = raw_session_id
    monkeypatch.setattr(user, 'ui', fake_ui)

    assert user._current_session_id() is None  # noqa: SLF001


def test_format_msat() -> None:
    assert user._format_msat(7_000) == '7 sats'  # noqa: SLF001
