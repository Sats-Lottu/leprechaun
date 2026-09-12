from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.ledger_client import (
    LedgerBalance,
    LedgerClient,
    LedgerHoldCreate,
    LedgerTransactionCreate,
    LedgerTransactionEntryCreate,
)
from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import (
    CheckoutSession,
    ConnectedApplication,
    UserLedgerAccount,
)
from hub.rabbitmq import request_invoice_rabbitmq


@dataclass(frozen=True)
class CheckoutFundingPlan:
    amount_msat: int
    available_balance_msat: int
    internal_amount_msat: int
    external_amount_msat: int


@dataclass(frozen=True)
class CheckoutPreparation:
    session: CheckoutSession
    balance: LedgerBalance
    plan: CheckoutFundingPlan


FINAL_CHECKOUT_STATUSES = {
    CheckoutSessionStatus.SETTLED,
    CheckoutSessionStatus.FAILED,
    CheckoutSessionStatus.CANCELED,
    CheckoutSessionStatus.EXPIRED,
}


class CheckoutSessionStateError(RuntimeError):
    def __init__(self, status: str) -> None:
        super().__init__(f'checkout session cannot be prepared from {status}')
        self.status = status


class CheckoutCancellationError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class CheckoutSettlementError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


async def get_or_create_user_ledger_account(
    *,
    user_sub: str,
    session: AsyncSession,
    ledger: LedgerClient,
) -> UserLedgerAccount:
    account = await session.scalar(
        select(UserLedgerAccount).where(UserLedgerAccount.user_sub == user_sub)
    )
    if account is not None:
        return account

    ledger_account = await ledger.create_user_account(user_sub)
    account = UserLedgerAccount(
        user_sub=user_sub,
        ledger_account_id=ledger_account.account_id,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def prepare_checkout_payment(
    *,
    checkout_session_id: UUID,
    user_sub: str,
    session: AsyncSession,
    ledger: LedgerClient | None = None,
    invoice_requester=request_invoice_rabbitmq,
) -> CheckoutPreparation:
    ledger = ledger or LedgerClient()
    checkout_session = await session.scalar(
        select(CheckoutSession).where(
            CheckoutSession.id == checkout_session_id
        )
    )
    if checkout_session is None:
        raise LookupError('checkout session not found')

    await ensure_application_active(checkout_session, session)

    if expire_if_due(checkout_session):
        await session.commit()
        await session.refresh(checkout_session)

    if checkout_session.status in FINAL_CHECKOUT_STATUSES:
        raise CheckoutSessionStateError(str(checkout_session.status))

    if checkout_session.user_id not in {None, user_sub}:
        raise CheckoutSessionStateError('owned by another user')

    account = await get_or_create_user_ledger_account(
        user_sub=user_sub,
        session=session,
        ledger=ledger,
    )
    balance = await ledger.get_balance(account.ledger_account_id)
    if checkout_session.status in {
        CheckoutSessionStatus.RESERVED,
        CheckoutSessionStatus.AWAITING_PAYMENT,
    }:
        plan = CheckoutFundingPlan(
            amount_msat=checkout_session.amount_msat,
            available_balance_msat=balance.available_balance_msat,
            internal_amount_msat=checkout_session.internal_amount_msat,
            external_amount_msat=checkout_session.external_amount_msat,
        )
    else:
        plan = funding_plan(
            amount_msat=checkout_session.amount_msat,
            available_balance_msat=balance.available_balance_msat,
        )

    checkout_session.user_id = user_sub
    checkout_session.ledger_account_id = account.ledger_account_id
    checkout_session.internal_amount_msat = plan.internal_amount_msat
    checkout_session.external_amount_msat = plan.external_amount_msat

    if plan.internal_amount_msat and checkout_session.ledger_hold_id is None:
        hold = await ledger.create_hold(
            LedgerHoldCreate(
                account_id=account.ledger_account_id,
                amount_msat=plan.internal_amount_msat,
                reason='checkout',
                reference_id=checkout_session.id,
                idempotency_key=f'checkout:{checkout_session.id}:hold',
                expires_at=checkout_session.expires_at,
            )
        )
        checkout_session.ledger_hold_id = hold.hold_id

    if (
        plan.external_amount_msat
        and not checkout_session.invoice_id
        and not checkout_session.payment_request
    ):
        invoice = await invoice_requester(
            user_id=user_sub,
            amount_msat=plan.external_amount_msat,
            memo=checkout_session.description,
        )
        if invoice:
            checkout_session.invoice_id = invoice.get('invoice_id')
            checkout_session.payment_request = invoice.get(
                'payment_request',
                '',
            )
            expires_at = invoice.get('expires_at')
            if expires_at:
                checkout_session.invoice_expires_at = datetime.fromisoformat(
                    str(expires_at)
                )

    checkout_session.status = (
        CheckoutSessionStatus.AWAITING_PAYMENT
        if plan.external_amount_msat
        else CheckoutSessionStatus.RESERVED
    )
    await session.commit()
    await session.refresh(checkout_session)

    return CheckoutPreparation(
        session=checkout_session,
        balance=balance,
        plan=plan,
    )


def funding_plan(
    *,
    amount_msat: int,
    available_balance_msat: int,
) -> CheckoutFundingPlan:
    internal_amount_msat = min(amount_msat, max(available_balance_msat, 0))
    external_amount_msat = amount_msat - internal_amount_msat
    return CheckoutFundingPlan(
        amount_msat=amount_msat,
        available_balance_msat=available_balance_msat,
        internal_amount_msat=internal_amount_msat,
        external_amount_msat=external_amount_msat,
    )


def expire_if_due(checkout_session: CheckoutSession) -> bool:
    if checkout_session.status in {
        CheckoutSessionStatus.SETTLED,
        CheckoutSessionStatus.FAILED,
        CheckoutSessionStatus.CANCELED,
        CheckoutSessionStatus.EXPIRED,
    }:
        return False

    expires_at = checkout_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at > datetime.now(UTC):
        return False

    checkout_session.status = CheckoutSessionStatus.EXPIRED
    return True


async def cancel_checkout_payment(
    *,
    checkout_session_id: UUID,
    session: AsyncSession,
    ledger: LedgerClient | None = None,
) -> CheckoutSession:
    ledger = ledger or LedgerClient()
    checkout_session = await session.scalar(
        select(CheckoutSession).where(
            CheckoutSession.id == checkout_session_id
        )
    )
    if checkout_session is None:
        raise LookupError('checkout session not found')

    if expire_if_due(checkout_session):
        await session.commit()
        await session.refresh(checkout_session)
        return checkout_session

    if checkout_session.status in FINAL_CHECKOUT_STATUSES:
        if checkout_session.status == CheckoutSessionStatus.CANCELED:
            return checkout_session
        raise CheckoutSessionStateError(str(checkout_session.status))

    if checkout_session.ledger_hold_id is not None:
        hold = await ledger.get_hold(checkout_session.ledger_hold_id)
        if hold.status == 'active':
            released_hold = await ledger.release_hold(
                hold.hold_id,
                idempotency_key=f'checkout:{checkout_session.id}:cancel',
            )
            checkout_session.ledger_hold_id = released_hold.hold_id
        elif hold.status not in {'released', 'expired'}:
            raise CheckoutCancellationError(
                f'checkout hold is {hold.status} and cannot be canceled'
            )

    checkout_session.status = CheckoutSessionStatus.CANCELED
    checkout_session.canceled_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(checkout_session)
    return checkout_session


async def settle_checkout_payment(
    *,
    checkout_session_id: UUID,
    session: AsyncSession,
    ledger: LedgerClient | None = None,
) -> CheckoutSession:
    ledger = ledger or LedgerClient()
    checkout_session = await session.scalar(
        select(CheckoutSession).where(
            CheckoutSession.id == checkout_session_id
        )
    )
    if checkout_session is None:
        raise LookupError('checkout session not found')

    await ensure_application_active(checkout_session, session)

    if expire_if_due(checkout_session):
        await session.commit()
        await session.refresh(checkout_session)
        raise CheckoutSessionStateError(str(checkout_session.status))

    if checkout_session.status in FINAL_CHECKOUT_STATUSES:
        if checkout_session.status == CheckoutSessionStatus.SETTLED:
            return checkout_session
        raise CheckoutSessionStateError(str(checkout_session.status))

    if checkout_session.ledger_account_id is None:
        raise CheckoutSettlementError('checkout session has no source account')
    if checkout_session.destination_account_id is None:
        raise CheckoutSettlementError(
            'checkout session has no destination account'
        )
    if (
        checkout_session.internal_amount_msat <= 0
        and checkout_session.external_amount_msat <= 0
    ):
        raise CheckoutSettlementError(
            'checkout session must be prepared before settlement'
        )

    balance = await ledger.get_balance(checkout_session.ledger_account_id)
    if (
        checkout_session.external_amount_msat > 0
        and balance.available_balance_msat
        < checkout_session.external_amount_msat
    ):
        raise CheckoutSettlementError(
            'checkout payment is not fully funded yet'
        )

    if checkout_session.internal_amount_msat > 0:
        if checkout_session.ledger_hold_id is None:
            raise CheckoutSettlementError(
                'checkout session has no internal hold to consume'
            )
        internal_txn = await ledger.create_transaction(
            LedgerTransactionCreate(
                reference_type='checkout_session',
                reference_id=checkout_session.id,
                idempotency_key=f'checkout:{checkout_session.id}:settle:internal',
                description='Checkout internal settlement',
                entries=(
                    LedgerTransactionEntryCreate(
                        account_id=checkout_session.ledger_account_id,
                        entry_type='debit',
                        amount_msat=checkout_session.internal_amount_msat,
                        description='Checkout reserved debit',
                        reference_type='checkout_session',
                        reference_id=checkout_session.id,
                    ),
                    LedgerTransactionEntryCreate(
                        account_id=checkout_session.destination_account_id,
                        entry_type='credit',
                        amount_msat=checkout_session.internal_amount_msat,
                        description='Checkout destination credit',
                        reference_type='checkout_session',
                        reference_id=checkout_session.id,
                    ),
                ),
            )
        )
        await ledger.consume_hold(
            checkout_session.ledger_hold_id,
            transaction_id=internal_txn.transaction_id,
            idempotency_key=f'checkout:{checkout_session.id}:consume:internal',
        )

    if checkout_session.external_amount_msat > 0:
        external_txn = await ledger.create_transaction(
            LedgerTransactionCreate(
                reference_type='checkout_session',
                reference_id=checkout_session.id,
                idempotency_key=f'checkout:{checkout_session.id}:settle:external',
                description='Checkout external settlement',
                entries=(
                    LedgerTransactionEntryCreate(
                        account_id=checkout_session.ledger_account_id,
                        entry_type='debit',
                        amount_msat=checkout_session.external_amount_msat,
                        description='Checkout external debit',
                        reference_type='checkout_session',
                        reference_id=checkout_session.id,
                    ),
                    LedgerTransactionEntryCreate(
                        account_id=checkout_session.destination_account_id,
                        entry_type='credit',
                        amount_msat=checkout_session.external_amount_msat,
                        description='Checkout destination credit',
                        reference_type='checkout_session',
                        reference_id=checkout_session.id,
                    ),
                ),
            )
        )
        await ledger.post_transaction(
            external_txn.transaction_id,
            idempotency_key=f'checkout:{checkout_session.id}:post:external',
        )

    checkout_session.status = CheckoutSessionStatus.SETTLED
    checkout_session.settled_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(checkout_session)
    return checkout_session


async def ensure_application_active(checkout_session, session) -> None:
    application_id = getattr(checkout_session, 'application_id', None)
    if application_id is not None:
        application = await session.get(ConnectedApplication, application_id)
        if application is None or not application.is_active:
            raise CheckoutSessionStateError('application disabled')
