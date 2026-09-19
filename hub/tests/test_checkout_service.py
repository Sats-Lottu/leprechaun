from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from hub.checkout_service import (
    CheckoutCancellationError,
    CheckoutSessionStateError,
    CheckoutSettlementError,
    cancel_checkout_payment,
    funding_plan,
    get_or_create_user_ledger_account,
    prepare_checkout_payment,
    settle_checkout_payment,
)
from hub.ledger_client import LedgerAccount, LedgerBalance, LedgerHold
from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import CheckoutSession

AMOUNT_MSAT = 10_000
INTERNAL_MSAT = 4_000
EXTERNAL_MSAT = 6_000
TRANSACTION_COUNT = 2


class FakeLedger:
    def __init__(self, available_balance_msat: int) -> None:
        self.available_balance_msat = available_balance_msat
        self.created_accounts: list[str] = []
        self.holds: list[int] = []

    async def create_user_account(self, user_sub: str) -> LedgerAccount:
        self.created_accounts.append(user_sub)
        return LedgerAccount(
            account_id=UUID('00000000-0000-0000-0000-000000000001')
        )

    async def get_balance(self, _account_id: UUID) -> LedgerBalance:
        return LedgerBalance(
            balance_msat=self.available_balance_msat,
            reserved_balance_msat=0,
            available_balance_msat=self.available_balance_msat,
        )

    async def create_hold(self, payload) -> LedgerHold:
        self.holds.append(payload.amount_msat)
        return LedgerHold(
            hold_id=UUID('00000000-0000-0000-0000-000000000002'),
            status='active',
        )


def test_funding_plan_uses_internal_balance_first() -> None:
    plan = funding_plan(
        amount_msat=AMOUNT_MSAT,
        available_balance_msat=INTERNAL_MSAT,
    )

    assert plan.internal_amount_msat == INTERNAL_MSAT
    assert plan.external_amount_msat == EXTERNAL_MSAT


async def test_lightning_selection_preserves_wallet_balance(session):
    checkout = CheckoutSession(
        game_id='game',
        order_id='lightning-only',
        amount_msat=AMOUNT_MSAT,
        description='Purchase',
        return_url='https://example.com/return',
        cancel_url='https://example.com/cancel',
        checkout_url='https://example.com/checkout',
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(checkout)
    await session.commit()
    ledger = FakeLedger(available_balance_msat=AMOUNT_MSAT)

    async def invoice_requester(**kwargs):
        assert kwargs['amount_msat'] == AMOUNT_MSAT
        return {'invoice_id': 'lightning', 'payment_request': 'lnbc1test'}

    result = await prepare_checkout_payment(
        checkout_session_id=checkout.id,
        user_sub='user-123',
        session=session,
        ledger=ledger,
        invoice_requester=invoice_requester,
        use_balance=False,
    )
    assert result.session.internal_amount_msat == 0
    assert result.session.external_amount_msat == AMOUNT_MSAT
    assert ledger.holds == []
    repeated = await prepare_checkout_payment(
        checkout_session_id=checkout.id,
        user_sub='user-123',
        session=session,
        ledger=ledger,
        invoice_requester=invoice_requester,
    )
    assert repeated.session.internal_amount_msat == 0
    assert ledger.holds == []


@pytest.mark.asyncio
async def test_get_or_create_user_ledger_account_persists_mapping(
    session,
) -> None:
    ledger = FakeLedger(available_balance_msat=0)

    account = await get_or_create_user_ledger_account(
        user_sub='user-123',
        session=session,
        ledger=ledger,
    )
    existing = await get_or_create_user_ledger_account(
        user_sub='user-123',
        session=session,
        ledger=ledger,
    )

    assert account.ledger_account_id == existing.ledger_account_id
    assert ledger.created_accounts == ['user-123']


@pytest.mark.asyncio
async def test_prepare_checkout_payment_creates_hold_and_invoice(
    session,
) -> None:
    checkout_session = CheckoutSession(
        game_id='leprechaun-game',
        order_id='order-1',
        amount_msat=AMOUNT_MSAT,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
        checkout_url='https://hub.example/user/checkout?session_id=1',
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    session.add(checkout_session)
    await session.commit()
    await session.refresh(checkout_session)
    ledger = FakeLedger(available_balance_msat=INTERNAL_MSAT)

    async def invoice_requester(**kwargs):
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
        return {
            'invoice_id': 'invoice-1',
            'payment_request': 'lnbc1...',
            'expires_at': expires_at.isoformat(),
            'amount_msat': kwargs['amount_msat'],
        }

    preparation = await prepare_checkout_payment(
        checkout_session_id=checkout_session.id,
        user_sub='user-123',
        session=session,
        ledger=ledger,
        invoice_requester=invoice_requester,
    )

    assert preparation.session.status == CheckoutSessionStatus.AWAITING_PAYMENT
    assert preparation.session.internal_amount_msat == INTERNAL_MSAT
    assert preparation.session.external_amount_msat == EXTERNAL_MSAT
    assert preparation.session.invoice_id == 'invoice-1'
    assert preparation.session.payment_request == 'lnbc1...'
    assert ledger.holds == [INTERNAL_MSAT]

    # The first hold removes these funds from the available balance.
    ledger.available_balance_msat = 0

    async def unexpected_invoice(**_kwargs):
        raise AssertionError('retry must reuse the existing invoice')

    repeated = await prepare_checkout_payment(
        checkout_session_id=checkout_session.id,
        user_sub='user-123',
        session=session,
        ledger=ledger,
        invoice_requester=unexpected_invoice,
    )
    assert repeated.plan.internal_amount_msat == INTERNAL_MSAT
    assert repeated.plan.external_amount_msat == EXTERNAL_MSAT
    assert ledger.holds == [INTERNAL_MSAT]

    with pytest.raises(CheckoutSessionStateError, match='another user'):
        await prepare_checkout_payment(
            checkout_session_id=checkout_session.id,
            user_sub='another-user',
            session=session,
            ledger=ledger,
            invoice_requester=unexpected_invoice,
        )
    assert checkout_session.user_id == 'user-123'
    assert ledger.created_accounts == ['user-123']


@pytest.mark.asyncio
async def test_prepare_checkout_payment_expires_before_side_effects(
    session,
) -> None:
    checkout_session = CheckoutSession(
        game_id='leprechaun-game',
        order_id='order-1',
        amount_msat=AMOUNT_MSAT,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
        checkout_url='https://hub.example/user/checkout?session_id=1',
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    session.add(checkout_session)
    await session.commit()
    await session.refresh(checkout_session)
    ledger = FakeLedger(available_balance_msat=AMOUNT_MSAT)

    async def invoice_requester(**_kwargs):
        raise AssertionError('invoice should not be requested')

    with pytest.raises(CheckoutSessionStateError) as exc_info:
        await prepare_checkout_payment(
            checkout_session_id=checkout_session.id,
            user_sub='user-123',
            session=session,
            ledger=ledger,
            invoice_requester=invoice_requester,
        )

    assert exc_info.value.status == CheckoutSessionStatus.EXPIRED
    assert ledger.created_accounts == []
    assert ledger.holds == []


@pytest.mark.asyncio
async def test_cancel_checkout_payment_releases_active_hold() -> None:
    checkout_session = SimpleNamespace(
        id=UUID('00000000-0000-0000-0000-000000000001'),
        status=CheckoutSessionStatus.AWAITING_PAYMENT,
        ledger_hold_id=UUID('00000000-0000-0000-0000-000000000002'),
        canceled_at=None,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    class FakeSession:
        def __init__(self) -> None:
            self.committed = False
            self.refreshed = False

        @staticmethod
        async def scalar(_query):
            return checkout_session

        async def commit(self) -> None:
            self.committed = True

        async def refresh(self, _item) -> None:
            self.refreshed = True

    class FakeLedger:
        def __init__(self) -> None:
            self.released = []

        @staticmethod
        async def get_hold(_hold_id):
            return SimpleNamespace(
                status='active',
                hold_id=checkout_session.ledger_hold_id,
            )

        async def release_hold(self, hold_id, *, idempotency_key=None):
            self.released.append((hold_id, idempotency_key))
            return SimpleNamespace(hold_id=hold_id, status='released')

    session = FakeSession()
    ledger = FakeLedger()

    result = await cancel_checkout_payment(
        checkout_session_id=checkout_session.id,
        session=session,
        ledger=ledger,
    )

    assert result.status == CheckoutSessionStatus.CANCELED
    assert ledger.released == [
        (
            checkout_session.ledger_hold_id,
            'checkout:00000000-0000-0000-0000-000000000001:cancel',
        )
    ]
    assert session.committed
    assert session.refreshed


@pytest.mark.asyncio
async def test_cancel_checkout_payment_rejects_consumed_hold() -> None:
    checkout_session = SimpleNamespace(
        id=UUID('00000000-0000-0000-0000-000000000001'),
        status=CheckoutSessionStatus.AWAITING_PAYMENT,
        ledger_hold_id=UUID('00000000-0000-0000-0000-000000000002'),
        canceled_at=None,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    class FakeSession:
        @staticmethod
        async def scalar(_query):
            return checkout_session

    class FakeLedger:
        @staticmethod
        async def get_hold(_hold_id):
            return SimpleNamespace(status='consumed')

    with pytest.raises(CheckoutCancellationError):
        await cancel_checkout_payment(
            checkout_session_id=checkout_session.id,
            session=FakeSession(),
            ledger=FakeLedger(),
        )


@pytest.mark.asyncio
async def test_settle_checkout_uses_hold_and_posts_external() -> None:
    checkout_session = SimpleNamespace(
        id=UUID('00000000-0000-0000-0000-000000000001'),
        status=CheckoutSessionStatus.AWAITING_PAYMENT,
        ledger_account_id=UUID('00000000-0000-0000-0000-000000000002'),
        destination_account_id=UUID('00000000-0000-0000-0000-000000000003'),
        ledger_hold_id=UUID('00000000-0000-0000-0000-000000000004'),
        internal_amount_msat=4_000,
        external_amount_msat=6_000,
        settled_at=None,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    class FakeSession:
        def __init__(self) -> None:
            self.committed = False

        @staticmethod
        async def scalar(_query):
            return checkout_session

        async def commit(self) -> None:
            self.committed = True

        @staticmethod
        async def refresh(_item) -> None:
            return None

    class FakeLedger:
        def __init__(self) -> None:
            self.created_transactions = []
            self.consumed = []
            self.posted = []

        @staticmethod
        async def get_balance(_account_id):
            return LedgerBalance(
                balance_msat=10_000,
                reserved_balance_msat=4_000,
                available_balance_msat=6_000,
            )

        async def create_transaction(self, payload):
            self.created_transactions.append(payload)
            transaction_id = UUID(
                '00000000-0000-0000-0000-000000000010'
                if len(self.created_transactions) == 1
                else '00000000-0000-0000-0000-000000000011'
            )
            return SimpleNamespace(
                transaction_id=transaction_id,
                status='pending',
            )

        async def consume_hold(
            self,
            hold_id,
            *,
            transaction_id,
            idempotency_key=None,
        ):
            self.consumed.append((hold_id, transaction_id, idempotency_key))
            return SimpleNamespace(hold_id=hold_id, status='consumed')

        async def post_transaction(
            self,
            transaction_id,
            *,
            idempotency_key=None,
        ):
            self.posted.append((transaction_id, idempotency_key))
            return SimpleNamespace(
                transaction_id=transaction_id,
                status='posted',
            )

    session = FakeSession()
    ledger = FakeLedger()

    result = await settle_checkout_payment(
        checkout_session_id=checkout_session.id,
        session=session,
        ledger=ledger,
    )

    assert result.status == CheckoutSessionStatus.SETTLED
    assert len(ledger.created_transactions) == TRANSACTION_COUNT
    assert ledger.consumed == [
        (
            checkout_session.ledger_hold_id,
            UUID('00000000-0000-0000-0000-000000000010'),
            'checkout:00000000-0000-0000-0000-000000000001:consume:internal',
        )
    ]
    assert ledger.posted == [
        (
            UUID('00000000-0000-0000-0000-000000000011'),
            'checkout:00000000-0000-0000-0000-000000000001:post:external',
        )
    ]
    assert session.committed


@pytest.mark.asyncio
async def test_settle_checkout_payment_requires_external_funds() -> None:
    checkout_session = SimpleNamespace(
        id=UUID('00000000-0000-0000-0000-000000000001'),
        status=CheckoutSessionStatus.AWAITING_PAYMENT,
        ledger_account_id=UUID('00000000-0000-0000-0000-000000000002'),
        destination_account_id=UUID('00000000-0000-0000-0000-000000000003'),
        ledger_hold_id=UUID('00000000-0000-0000-0000-000000000004'),
        internal_amount_msat=4_000,
        external_amount_msat=6_000,
        settled_at=None,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    class FakeSession:
        @staticmethod
        async def scalar(_query):
            return checkout_session

    class FakeLedger:
        @staticmethod
        async def get_balance(_account_id):
            return LedgerBalance(
                balance_msat=4_000,
                reserved_balance_msat=4_000,
                available_balance_msat=0,
            )

    with pytest.raises(CheckoutSettlementError):
        await settle_checkout_payment(
            checkout_session_id=checkout_session.id,
            session=FakeSession(),
            ledger=FakeLedger(),
        )
