import logging
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.accounts import get_account_or_404
from ledger.audit import AuditEvent, record_audit_event
from ledger.balances import available_balance, release_reserved, reserve
from ledger.errors import ledger_error
from ledger.hold_expiration import expire_due_holds_in_session
from ledger.hold_helpers import (
    ensure_active_hold,
    expire_hold_balance,
    get_hold_or_404,
    hold_details,
    hold_status_response_from_operation,
    is_hold_expired,
)
from ledger.idempotency import (
    OperationIdempotencyLookup,
    OperationIdempotencyRecord,
    get_operation_idempotency,
    record_operation_idempotency,
)
from ledger.models.database import get_session
from ledger.models.enums import HoldStatus
from ledger.models.tables import BalanceHold
from ledger.observability import log_event
from ledger.payload_hash import payload_hash
from ledger.routes.transactions import post_transaction_using_reserved_hold
from ledger.schemas import (
    AccountHolds,
    ExpireHoldsResponse,
    HoldCreate,
    HoldCreated,
    HoldDetails,
    HoldOperation,
    HoldStatusResponse,
)

router = APIRouter(prefix="/holds", tags=["holds"])
logger = logging.getLogger(__name__)

Session = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=HoldCreated,
)
async def create_hold(
    session: Session,
    payload: Annotated[HoldCreate, Body()],
):
    idempotency_payload_hash = payload_hash(payload)
    if payload.idempotency_key:
        existing_hold = await session.scalar(
            select(BalanceHold).where(
                BalanceHold.idempotency_key == payload.idempotency_key
            )
        )
        if existing_hold:
            existing_payload_hash = getattr(
                existing_hold, "idempotency_payload_hash", None
            )
            if existing_payload_hash and (
                existing_payload_hash != idempotency_payload_hash
            ):
                raise ledger_error(
                    status_code=status.HTTP_409_CONFLICT,
                    code="idempotency_payload_mismatch",
                    message=(
                        "Idempotency key was reused with a different payload"
                    ),
                )
            return HoldCreated(
                hold_id=existing_hold.id,
                status=existing_hold.status,
            )

    if payload.amount <= 0:
        raise ledger_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="invalid_hold_amount",
            message="Hold amount must be greater than zero",
        )

    account = await get_account_or_404(
        payload.account_id, session, for_update=True
    )
    if available_balance(account) < payload.amount:
        raise ledger_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="insufficient_available_balance",
            message="Insufficient available balance",
        )

    hold = BalanceHold(
        account_id=payload.account_id,
        amount=payload.amount,
        reason=payload.reason,
        reference_type=payload.reference_type,
        reference_id=payload.reference_id,
        idempotency_key=payload.idempotency_key,
        idempotency_payload_hash=idempotency_payload_hash,
        expires_at=payload.expires_at,
    )
    reserve(account, payload.amount)
    session.add(hold)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if payload.idempotency_key:
            existing_hold = await session.scalar(
                select(BalanceHold).where(
                    BalanceHold.idempotency_key == payload.idempotency_key
                )
            )
            if existing_hold:
                existing_payload_hash = getattr(
                    existing_hold, "idempotency_payload_hash", None
                )
                if existing_payload_hash and (
                    existing_payload_hash != idempotency_payload_hash
                ):
                    raise ledger_error(
                        status_code=status.HTTP_409_CONFLICT,
                        code="idempotency_payload_mismatch",
                        message=(
                            "Idempotency key was reused with a different "
                            "payload"
                        ),
                    ) from exc
                return HoldCreated(
                    hold_id=existing_hold.id,
                    status=existing_hold.status,
                )
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="hold_idempotency_conflict",
            message="Hold idempotency key already exists",
        ) from exc

    await session.refresh(hold)
    record_audit_event(
        session,
        AuditEvent(
            event_type="hold_created",
            resource_type="hold",
            resource_id=hold.id,
            idempotency_key=payload.idempotency_key,
            metadata={
                "account_id": str(hold.account_id),
                "amount": hold.amount,
                "reference_type": hold.reference_type,
                "reference_id": str(hold.reference_id)
                if hold.reference_id
                else None,
            },
        ),
    )
    await session.commit()
    log_event(
        logger,
        logging.INFO,
        "hold_created",
        hold_id=hold.id,
        account_id=hold.account_id,
        amount=hold.amount,
        expires_at=hold.expires_at,
    )
    return HoldCreated(hold_id=hold.id, status=hold.status)


@router.get("/{hold_id}", response_model=HoldDetails)
async def get_hold(hold_id: UUID, session: Session):
    hold = await get_hold_or_404(hold_id, session)
    return hold_details(hold)


@router.get("/account/{account_id}", response_model=AccountHolds)
async def list_account_holds(account_id: UUID, session: Session):
    await get_account_or_404(account_id, session)
    holds = await session.scalars(
        select(BalanceHold).where(BalanceHold.account_id == account_id)
    )
    return AccountHolds(
        account_id=account_id,
        holds=[hold_details(hold) for hold in holds],
    )


@router.post("/expire-due", response_model=ExpireHoldsResponse)
async def expire_due_holds(session: Session):
    expired_count = await expire_due_holds_in_session(session)
    return ExpireHoldsResponse(expired_count=expired_count)


@router.post("/{hold_id}/expire", response_model=HoldStatusResponse)
async def expire_hold(
    hold_id: UUID,
    session: Session,
    payload: Annotated[HoldOperation | None, Body()] = None,
):
    payload = payload or HoldOperation()
    operation_payload_hash = payload_hash(payload)
    existing_operation = await get_operation_idempotency(
        session,
        OperationIdempotencyLookup(
            operation="hold.expire",
            resource_id=hold_id,
            idempotency_key=payload.idempotency_key,
            payload_hash=operation_payload_hash,
        ),
        for_update=True,
    )
    if existing_operation:
        return hold_status_response_from_operation(existing_operation)

    hold = await get_hold_or_404(hold_id, session, for_update=True)
    ensure_active_hold(hold, "expired")

    account = await get_account_or_404(
        hold.account_id, session, for_update=True
    )
    expire_hold_balance(hold, account)
    record_operation_idempotency(
        session,
        OperationIdempotencyRecord(
            operation="hold.expire",
            resource_id=hold.id,
            idempotency_key=payload.idempotency_key,
            result_status=HoldStatus.EXPIRED,
            payload_hash=operation_payload_hash,
        ),
    )
    record_audit_event(
        session,
        AuditEvent(
            event_type="hold_expired",
            resource_type="hold",
            resource_id=hold.id,
            idempotency_key=payload.idempotency_key,
            metadata={
                "account_id": str(hold.account_id),
                "amount": hold.amount,
            },
        ),
    )
    await session.commit()
    log_event(
        logger,
        logging.INFO,
        "hold_expired",
        hold_id=hold.id,
        account_id=hold.account_id,
        amount=hold.amount,
    )
    return HoldStatusResponse(hold_id=hold.id, status=hold.status)


@router.post("/{hold_id}/consume", response_model=HoldStatusResponse)
async def consume_hold(
    hold_id: UUID,
    session: Session,
    payload: Annotated[HoldOperation | None, Body()] = None,
):
    payload = payload or HoldOperation()
    operation_payload_hash = payload_hash(payload)
    existing_operation = await get_operation_idempotency(
        session,
        OperationIdempotencyLookup(
            operation="hold.consume",
            resource_id=hold_id,
            idempotency_key=payload.idempotency_key,
            payload_hash=operation_payload_hash,
        ),
        for_update=True,
    )
    if existing_operation:
        return hold_status_response_from_operation(existing_operation)

    hold = await get_hold_or_404(hold_id, session, for_update=True)
    if hold.status == HoldStatus.ACTIVE and is_hold_expired(hold):
        account = await get_account_or_404(
            hold.account_id, session, for_update=True
        )
        expire_hold_balance(hold, account)
        await session.commit()
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="hold_expired",
            message="Hold has expired",
        )
    ensure_active_hold(hold, "consumed")

    if payload.transaction_id is None:
        raise ledger_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="hold_consume_transaction_required",
            message="A transaction_id is required to consume a hold",
        )

    transaction = await post_transaction_using_reserved_hold(
        payload.transaction_id,
        hold,
        session,
    )
    hold.status = HoldStatus.CONSUMED
    hold.consumed_at = datetime.now(UTC)
    record_operation_idempotency(
        session,
        OperationIdempotencyRecord(
            operation="hold.consume",
            resource_id=hold.id,
            idempotency_key=payload.idempotency_key,
            result_status=HoldStatus.CONSUMED,
            payload_hash=operation_payload_hash,
        ),
    )
    record_audit_event(
        session,
        AuditEvent(
            event_type="hold_consumed",
            resource_type="hold",
            resource_id=hold.id,
            idempotency_key=payload.idempotency_key,
            metadata={
                "account_id": str(hold.account_id),
                "amount": hold.amount,
                "transaction_id": str(transaction.id),
            },
        ),
    )

    await session.commit()
    log_event(
        logger,
        logging.INFO,
        "hold_consumed",
        hold_id=hold.id,
        account_id=hold.account_id,
        amount=hold.amount,
    )
    return HoldStatusResponse(hold_id=hold.id, status=hold.status)


@router.post("/{hold_id}/release", response_model=HoldStatusResponse)
async def release_hold(
    hold_id: UUID,
    session: Session,
    payload: Annotated[HoldOperation | None, Body()] = None,
):
    payload = payload or HoldOperation()
    operation_payload_hash = payload_hash(payload)
    existing_operation = await get_operation_idempotency(
        session,
        OperationIdempotencyLookup(
            operation="hold.release",
            resource_id=hold_id,
            idempotency_key=payload.idempotency_key,
            payload_hash=operation_payload_hash,
        ),
        for_update=True,
    )
    if existing_operation:
        return hold_status_response_from_operation(existing_operation)

    hold = await get_hold_or_404(hold_id, session, for_update=True)
    if hold.status == HoldStatus.ACTIVE and is_hold_expired(hold):
        account = await get_account_or_404(
            hold.account_id, session, for_update=True
        )
        expire_hold_balance(hold, account)
        await session.commit()
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="hold_expired",
            message="Hold has expired",
        )
    ensure_active_hold(hold, "released")

    account = await get_account_or_404(
        hold.account_id, session, for_update=True
    )
    release_reserved(account, hold.amount)
    hold.status = HoldStatus.RELEASED
    hold.released_at = datetime.now(UTC)
    record_operation_idempotency(
        session,
        OperationIdempotencyRecord(
            operation="hold.release",
            resource_id=hold.id,
            idempotency_key=payload.idempotency_key,
            result_status=HoldStatus.RELEASED,
            payload_hash=operation_payload_hash,
        ),
    )
    record_audit_event(
        session,
        AuditEvent(
            event_type="hold_released",
            resource_type="hold",
            resource_id=hold.id,
            idempotency_key=payload.idempotency_key,
            metadata={
                "account_id": str(hold.account_id),
                "amount": hold.amount,
            },
        ),
    )

    await session.commit()
    log_event(
        logger,
        logging.INFO,
        "hold_released",
        hold_id=hold.id,
        account_id=hold.account_id,
        amount=hold.amount,
    )
    return HoldStatusResponse(hold_id=hold.id, status=hold.status)
