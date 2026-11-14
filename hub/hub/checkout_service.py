from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hub.ledger_client import (
    LedgerBalance,
    LedgerClient,
    LedgerClientError,
    LedgerHoldCreate,
)
from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import (
    CheckoutSession,
    ConnectedApplication,
    UserLedgerAccount,
)
from hub.rabbitmq import request_invoice_rabbitmq
from hub.schemas import MSATS_PER_SAT


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
CHECKOUT_HOLD_GRACE = timedelta(minutes=5)


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
    commit: bool = True,
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
    insert = (
        pg_insert
        if session.bind.dialect.name == 'postgresql'
        else sqlite_insert
    )
    await session.execute(
        insert(UserLedgerAccount)
        .values(
            id=account.id,
            user_sub=user_sub,
            ledger_account_id=account.ledger_account_id,
        )
        .on_conflict_do_nothing(index_elements=['user_sub'])
    )
    if commit:
        await session.commit()
    return (
        await session.scalars(
            select(UserLedgerAccount).where(
                UserLedgerAccount.user_sub == user_sub
            )
        )
    ).one()


async def prepare_checkout_payment(  # noqa: PLR0913
    *,
    checkout_session_id: UUID,
    user_sub: str,
    session: AsyncSession,
    ledger: LedgerClient | None = None,
    invoice_requester=request_invoice_rabbitmq,
    use_balance: bool = True,
) -> CheckoutPreparation:
    ledger = ledger or LedgerClient()
    checkout_session = await lock_checkout(checkout_session_id, session)

    await ensure_application_active(checkout_session, session)
    if (
        checkout_session.status == CheckoutSessionStatus.CREATED
        and expire_if_due(checkout_session)
    ):
        await session.commit()
        await session.refresh(checkout_session)

    if checkout_session.status in FINAL_CHECKOUT_STATUSES:
        raise CheckoutSessionStateError(str(checkout_session.status))

    if checkout_session.user_id not in {None, user_sub}:
        raise CheckoutSessionStateError('owned by another user')

    account = await get_or_create_user_ledger_account(
        user_sub=user_sub, session=session, ledger=ledger, commit=False
    )
    balance = await ledger.get_balance(account.ledger_account_id)
    if checkout_session.status == CheckoutSessionStatus.CREATED:
        plan = funding_plan(
            amount_msat=checkout_session.amount_msat,
            available_balance_msat=balance.available_balance_msat
            if use_balance
            else 0,
        )
        checkout_session.user_id = user_sub
        checkout_session.ledger_account_id = account.ledger_account_id
        checkout_session.internal_amount_msat = plan.internal_amount_msat
        checkout_session.external_amount_msat = plan.external_amount_msat
        checkout_session.status = (
            CheckoutSessionStatus.AWAITING_PAYMENT
            if plan.external_amount_msat
            else CheckoutSessionStatus.RESERVED
        )
        await session.commit()
        checkout_session = await lock_checkout(checkout_session_id, session)
    else:
        plan = CheckoutFundingPlan(
            amount_msat=checkout_session.amount_msat,
            available_balance_msat=balance.available_balance_msat,
            internal_amount_msat=checkout_session.internal_amount_msat,
            external_amount_msat=checkout_session.external_amount_msat,
        )

    if checkout_session.status not in FINAL_CHECKOUT_STATUSES:
        if not checkout_is_due(checkout_session):
            await ensure_checkout_funding(
                checkout_session, session, ledger, invoice_requester
            )

    return CheckoutPreparation(
        session=checkout_session, balance=balance, plan=plan
    )


async def ensure_checkout_funding(
    checkout: CheckoutSession,
    session: AsyncSession,
    ledger: LedgerClient,
    invoice_requester=request_invoice_rabbitmq,
) -> None:
    if checkout.internal_amount_msat and checkout.ledger_hold_id is None:
        try:
            hold = await ledger.create_hold(
                LedgerHoldCreate(
                    account_id=checkout.ledger_account_id,
                    amount_msat=checkout.internal_amount_msat,
                    reason='checkout',
                    reference_type='checkout_session',
                    reference_id=checkout.id,
                    idempotency_key=f'checkout:{checkout.id}:hold',
                    expires_at=checkout.expires_at + CHECKOUT_HOLD_GRACE
                    if checkout.external_amount_msat
                    else checkout.expires_at,
                )
            )
        except LedgerClientError as exc:
            if exc.code != 'insufficient_available_balance':
                raise
            checkout.status = CheckoutSessionStatus.FAILED
            checkout.failed_at = datetime.now(UTC)
            await session.commit()
            return
        checkout.ledger_hold_id = hold.hold_id
        await session.flush()

    if checkout.external_amount_msat and not checkout.invoice_id:
        invoice = await invoice_requester(
            user_id=checkout.user_id,
            amount_msat=checkout.external_amount_msat,
            memo=checkout.description,
            operation_id=str(checkout.id),
        )
        if invoice:
            checkout.invoice_id = invoice.get('invoice_id')
            checkout.payment_request = invoice.get('payment_request') or ''
            if expires_at := invoice.get('expires_at'):
                checkout.invoice_expires_at = datetime.fromisoformat(
                    str(expires_at)
                )
    await session.commit()


def funding_plan(
    *,
    amount_msat: int,
    available_balance_msat: int,
) -> CheckoutFundingPlan:
    internal_amount_msat = min(amount_msat, max(available_balance_msat, 0))
    external_amount_msat = amount_msat - internal_amount_msat
    if external_amount_msat and amount_msat % MSATS_PER_SAT == 0:
        internal_amount_msat -= internal_amount_msat % MSATS_PER_SAT
        external_amount_msat = amount_msat - internal_amount_msat
    return CheckoutFundingPlan(
        amount_msat=amount_msat,
        available_balance_msat=available_balance_msat,
        internal_amount_msat=internal_amount_msat,
        external_amount_msat=external_amount_msat,
    )


def expire_if_due(checkout_session: CheckoutSession) -> bool:
    if checkout_session.status in FINAL_CHECKOUT_STATUSES:
        return False

    if not checkout_is_due(checkout_session):
        return False

    checkout_session.status = CheckoutSessionStatus.EXPIRED
    return True


async def cancel_checkout_payment(
    *,
    checkout_session_id: UUID,
    session: AsyncSession,
    ledger: LedgerClient | None = None,
    user_sub: str | None = None,
) -> CheckoutSession:
    ledger = ledger or LedgerClient()
    checkout_session = await lock_checkout(checkout_session_id, session)
    if user_sub is not None and checkout_session.user_id not in {
        None,
        user_sub,
    }:
        raise CheckoutSessionStateError('owned by another user')

    await ensure_cancelable(checkout_session, session, ledger)
    if (
        checkout_session.status not in FINAL_CHECKOUT_STATUSES
        and checkout_is_due(checkout_session)
    ):
        await expire_checkout(checkout_session, session, ledger)
        return checkout_session

    if checkout_session.status in FINAL_CHECKOUT_STATUSES:
        if checkout_session.status == CheckoutSessionStatus.CANCELED:
            return checkout_session
        raise CheckoutSessionStateError(str(checkout_session.status))

    await release_checkout_hold(checkout_session, ledger, action='cancel')
    checkout_session.status = CheckoutSessionStatus.CANCELED
    checkout_session.canceled_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(checkout_session)
    return checkout_session


async def ensure_cancelable(
    checkout: CheckoutSession,
    session: AsyncSession,
    ledger: LedgerClient,
) -> None:
    if checkout.status not in {
        CheckoutSessionStatus.RESERVED,
        CheckoutSessionStatus.AWAITING_PAYMENT,
    }:
        return
    settlement = await ledger.checkout_settlement(checkout.id)
    legacy_complete = (
        not checkout.internal_amount_msat or settlement['legacy_internal']
    ) and (not checkout.external_amount_msat or settlement['legacy_external'])
    if settlement['settled'] or legacy_complete:
        await mark_settled(checkout, session)
        raise CheckoutSessionStateError(str(checkout.status))
    if settlement['legacy_internal'] or settlement['legacy_external']:
        raise CheckoutCancellationError(
            'Checkout already has a posted payment'
        )


async def release_checkout_hold(
    checkout: CheckoutSession,
    ledger: LedgerClient,
    *,
    action: str,
) -> None:
    if not checkout.internal_amount_msat:
        return
    hold = (
        await ledger.get_hold(checkout.ledger_hold_id)
        if checkout.ledger_hold_id
        else await ledger.checkout_hold(checkout.id)
    )
    if hold is None or hold.status in {'released', 'expired'}:
        return
    if hold.status != 'active':
        raise CheckoutCancellationError(f'Checkout hold is {hold.status}')
    try:
        await ledger.release_hold(
            hold.hold_id,
            idempotency_key=f'checkout:{checkout.id}:{action}',
        )
    except LedgerClientError as exc:
        if exc.code != 'hold_expired':
            raise


async def settle_checkout_payment(
    *,
    checkout_session_id: UUID,
    session: AsyncSession,
    ledger: LedgerClient | None = None,
) -> CheckoutSession:
    ledger = ledger or LedgerClient()
    checkout_session = await lock_checkout(checkout_session_id, session)

    if checkout_session.status in FINAL_CHECKOUT_STATUSES:
        if checkout_session.status == CheckoutSessionStatus.SETTLED:
            return checkout_session
        raise CheckoutSessionStateError(str(checkout_session.status))
    try:
        await ensure_application_active(checkout_session, session)
    except CheckoutSessionStateError:
        if (await ledger.checkout_settlement(checkout_session.id))['settled']:
            await mark_settled(checkout_session, session)
            return checkout_session
        if expire_if_due(checkout_session):
            await session.commit()
        raise
    if (
        checkout_session.status == CheckoutSessionStatus.CREATED
        or checkout_session.ledger_account_id is None
        or checkout_session.destination_account_id is None
    ):
        raise CheckoutSettlementError(
            'checkout session must be prepared before settlement'
        )
    if (
        checkout_session.internal_amount_msat
        and not checkout_session.ledger_hold_id
    ):
        raise CheckoutSettlementError('checkout hold is being prepared')

    try:
        await ledger.settle_checkout(
            checkout_session.id,
            {
                'source_account_id': str(checkout_session.ledger_account_id),
                'destination_account_id': str(
                    checkout_session.destination_account_id
                ),
                'internal_amount_msat': checkout_session.internal_amount_msat,
                'external_amount_msat': checkout_session.external_amount_msat,
                'hold_id': str(checkout_session.ledger_hold_id)
                if checkout_session.ledger_hold_id
                else None,
                'invoice_id': checkout_session.invoice_id,
                'expires_at': checkout_session.expires_at.isoformat(),
            },
        )
    except LedgerClientError as exc:
        if exc.code == 'checkout_expired':
            await ensure_cancelable(checkout_session, session, ledger)
            await expire_checkout(checkout_session, session, ledger)
            return checkout_session
        if exc.code in {
            'checkout_invoice_unconfirmed',
            'checkout_funds_unavailable',
        }:
            raise CheckoutSettlementError(str(exc)) from exc
        raise

    await mark_settled(checkout_session, session)
    return checkout_session


async def mark_settled(
    checkout: CheckoutSession, session: AsyncSession
) -> None:
    checkout.status = CheckoutSessionStatus.SETTLED
    checkout.settled_at = datetime.now(UTC)
    await session.commit()


async def expire_checkout(
    checkout: CheckoutSession,
    session: AsyncSession,
    ledger: LedgerClient,
) -> None:
    if not checkout_is_due(checkout):
        return
    await release_checkout_hold(checkout, ledger, action='expire')
    expire_if_due(checkout)
    await session.commit()


def checkout_is_due(checkout: CheckoutSession) -> bool:
    expires_at = checkout.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


async def lock_checkout(
    checkout_id: UUID, session: AsyncSession
) -> CheckoutSession:
    checkout = await session.scalar(
        select(CheckoutSession)
        .where(CheckoutSession.id == checkout_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if checkout is None:
        raise LookupError('checkout session not found')
    return checkout


async def read_checkout(
    checkout_id: UUID,
    session: AsyncSession,
    *,
    user_sub: str | None = None,
) -> CheckoutSession | None:
    checkout = await session.get(CheckoutSession, checkout_id)
    if checkout is None or (
        user_sub is not None and checkout.user_id not in {None, user_sub}
    ):
        return None
    return checkout


async def resume_checkout(checkout_id: UUID, session: AsyncSession) -> None:
    ledger = LedgerClient()
    checkout = await lock_checkout(checkout_id, session)
    if checkout.status not in {
        CheckoutSessionStatus.RESERVED,
        CheckoutSessionStatus.AWAITING_PAYMENT,
    }:
        return
    missing_hold = (
        checkout.internal_amount_msat and not checkout.ledger_hold_id
    )
    missing_invoice = checkout.external_amount_msat and not checkout.invoice_id
    if checkout_is_due(checkout) and missing_hold:
        await expire_checkout(checkout, session, ledger)
        return
    if not checkout_is_due(checkout) and (missing_hold or missing_invoice):
        try:
            await ensure_application_active(checkout, session)
        except CheckoutSessionStateError:
            return
        await ensure_checkout_funding(checkout, session, ledger)
        if checkout.status in FINAL_CHECKOUT_STATUSES:
            return
    try:
        await settle_checkout_payment(
            checkout_session_id=checkout_id,
            session=session,
            ledger=ledger,
        )
    except (CheckoutSessionStateError, CheckoutSettlementError):
        return


async def expire_unconfirmed_checkouts(session: AsyncSession) -> None:
    await session.execute(
        update(CheckoutSession)
        .where(
            CheckoutSession.status == CheckoutSessionStatus.CREATED,
            CheckoutSession.expires_at <= datetime.now(UTC),
        )
        .values(status=CheckoutSessionStatus.EXPIRED)
    )
    await session.commit()


async def ensure_application_active(checkout_session, session) -> None:
    application_id = getattr(checkout_session, 'application_id', None)
    if application_id is not None:
        application = await session.get(ConnectedApplication, application_id)
        if application is None or not application.is_active:
            raise CheckoutSessionStateError('application disabled')
