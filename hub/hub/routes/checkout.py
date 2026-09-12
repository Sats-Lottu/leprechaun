from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hub.applications import (
    require_application,
    require_checkout_user,
    validate_checkout_application,
)
from hub.checkout_service import (
    CheckoutCancellationError,
    CheckoutSessionStateError,
    CheckoutSettlementError,
    cancel_checkout_payment,
    expire_if_due,
    prepare_checkout_payment,
    settle_checkout_payment,
)
from hub.ledger_client import LedgerClientError
from hub.models.database import get_session
from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import CheckoutSession as CheckoutSessionTable
from hub.models.tables import ConnectedApplication
from hub.schemas import (
    MSATS_PER_SAT,
    CheckoutSessionResponse,
    CreateCheckoutSessionRequest,
)

router = APIRouter(prefix='/api/checkout', tags=['checkout'])
Application = Annotated[ConnectedApplication, Depends(require_application)]
CheckoutUser = Annotated[str, Depends(require_checkout_user)]


@dataclass(frozen=True)
class CheckoutSession:
    session_id: str
    status: str
    user_id: str | None
    game_id: str
    order_id: str
    destination_account_id: str | None
    amount_msat: int
    internal_amount_msat: int
    external_amount_msat: int
    description: str
    return_url: HttpUrl
    cancel_url: HttpUrl
    checkout_url: str
    ledger_hold_id: str | None
    invoice_id: str | None
    payment_request: str
    created_at: str
    expires_at: str

    def to_response(self) -> CheckoutSessionResponse:
        return CheckoutSessionResponse(
            session_id=self.session_id,
            status=self.status,
            user_id=self.user_id,
            game_id=self.game_id,
            order_id=self.order_id,
            destination_account_id=self.destination_account_id,
            amount_sats=self.amount_msat // MSATS_PER_SAT,
            internal_amount_sats=self.internal_amount_msat // MSATS_PER_SAT,
            external_amount_sats=self.external_amount_msat // MSATS_PER_SAT,
            description=self.description,
            return_url=self.return_url,
            cancel_url=self.cancel_url,
            checkout_url=self.checkout_url,
            ledger_hold_id=self.ledger_hold_id,
            invoice_id=self.invoice_id,
            payment_request=self.payment_request,
            created_at=self.created_at,
            expires_at=self.expires_at,
        )


@dataclass(frozen=True)
class CreateCheckoutSessionResult:
    session: CheckoutSession
    created: bool


@router.post(
    '/sessions',
    status_code=status.HTTP_201_CREATED,
)
async def create_checkout_session(
    payload: CreateCheckoutSessionRequest,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    application: Application,
) -> CheckoutSessionResponse:
    validate_checkout_application(payload, application)
    result = await create_session(
        payload,
        base_url=str(request.base_url),
        session=session,
        application_id=application.id,
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK

    return result.session.to_response()


@router.get('/sessions/{session_id}')
async def get_checkout_session(
    session_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    application: Application,
) -> CheckoutSessionResponse:
    checkout_session = await _get_checkout_session(
        session,
        CheckoutSessionTable.id == session_id,
        CheckoutSessionTable.application_id == application.id,
    )
    if expire_if_due(checkout_session):
        await session.commit()
        await session.refresh(checkout_session)
    return _to_domain(checkout_session).to_response()


@router.get('/orders/{game_id}/{order_id}')
async def get_checkout_order(
    game_id: str,
    order_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    application: Application,
) -> CheckoutSessionResponse:
    checkout_session = await _get_checkout_session(
        session,
        CheckoutSessionTable.game_id == game_id,
        CheckoutSessionTable.order_id == order_id,
        CheckoutSessionTable.application_id == application.id,
    )
    if expire_if_due(checkout_session):
        await session.commit()
        await session.refresh(checkout_session)
    return _to_domain(checkout_session).to_response()


@router.post('/sessions/{session_id}/prepare')
async def prepare_checkout_session(
    session_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_sub: CheckoutUser,
) -> CheckoutSessionResponse:
    try:
        preparation = await prepare_checkout_payment(
            checkout_session_id=session_id,
            user_sub=user_sub,
            session=session,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Checkout session not found',
        ) from exc
    except CheckoutSessionStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Checkout session is {exc.status}',
        ) from exc

    return _to_domain(preparation.session).to_response()


@router.post('/sessions/{session_id}/cancel')
async def cancel_checkout_session(
    session_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    application: Application,
) -> CheckoutSessionResponse:
    await _get_checkout_session(
        session,
        CheckoutSessionTable.id == session_id,
        CheckoutSessionTable.application_id == application.id,
    )
    try:
        checkout_session = await cancel_checkout_payment(
            checkout_session_id=session_id,
            session=session,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Checkout session not found',
        ) from exc
    except CheckoutSessionStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Checkout session is {exc.status}',
        ) from exc
    except CheckoutCancellationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except LedgerClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Could not cancel ledger hold',
        ) from exc

    return _to_domain(checkout_session).to_response()


@router.post('/sessions/{session_id}/settle')
async def settle_checkout_session(
    session_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user_sub: CheckoutUser,
) -> CheckoutSessionResponse:
    await _get_checkout_session(
        session,
        CheckoutSessionTable.id == session_id,
        CheckoutSessionTable.user_id == user_sub,
    )
    try:
        checkout_session = await settle_checkout_payment(
            checkout_session_id=session_id,
            session=session,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Checkout session not found',
        ) from exc
    except CheckoutSessionStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Checkout session is {exc.status}',
        ) from exc
    except CheckoutSettlementError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except LedgerClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Could not settle checkout in ledger',
        ) from exc

    return _to_domain(checkout_session).to_response()


async def create_session(
    payload: CreateCheckoutSessionRequest,
    *,
    base_url: str,
    session: AsyncSession,
    now: datetime | None = None,
    application_id: UUID | None = None,
) -> CreateCheckoutSessionResult:
    existing_session = await session.scalar(
        select(CheckoutSessionTable).where(
            CheckoutSessionTable.game_id == payload.game_id,
            CheckoutSessionTable.order_id == payload.order_id,
        )
    )
    if existing_session is not None:
        expected = {
            'application_id': application_id,
            'amount_msat': payload.amount_msat(),
            'destination_account_id': payload.destination_account_id,
            'description': payload.description,
            'return_url': str(payload.return_url),
            'cancel_url': str(payload.cancel_url),
        }
        if payload.user_id is not None:
            expected['user_id'] = payload.user_id
        if any(
            getattr(existing_session, field) != value
            for field, value in expected.items()
        ):
            raise HTTPException(
                409, 'Order already exists with different details'
            )
        return CreateCheckoutSessionResult(
            session=_to_domain(existing_session),
            created=False,
        )

    now = now or datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=payload.expires_in_sec)
    session_id = uuid4()
    checkout_url = _build_checkout_url(base_url, str(session_id))

    checkout_session = CheckoutSessionTable(
        application_id=application_id,
        status=CheckoutSessionStatus.CREATED,
        user_id=payload.user_id,
        game_id=payload.game_id,
        order_id=payload.order_id,
        destination_account_id=payload.destination_account_id,
        amount_msat=payload.amount_msat(),
        description=payload.description,
        return_url=str(payload.return_url),
        cancel_url=str(payload.cancel_url),
        checkout_url=checkout_url,
        expires_at=expires_at,
    )
    checkout_session.id = session_id
    session.add(checkout_session)
    await session.commit()
    await session.refresh(checkout_session)

    return CreateCheckoutSessionResult(
        session=_to_domain(checkout_session),
        created=True,
    )


async def _get_checkout_session(
    session: AsyncSession,
    *conditions,
) -> CheckoutSessionTable:
    checkout_session = await session.scalar(
        select(CheckoutSessionTable).where(*conditions)
    )
    if checkout_session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Checkout session not found',
        )
    return checkout_session


def _to_domain(session: CheckoutSessionTable) -> CheckoutSession:
    return CheckoutSession(
        session_id=str(session.id),
        status=session.status,
        user_id=session.user_id,
        game_id=session.game_id,
        order_id=session.order_id,
        destination_account_id=str(session.destination_account_id)
        if session.destination_account_id
        else None,
        amount_msat=session.amount_msat,
        internal_amount_msat=session.internal_amount_msat,
        external_amount_msat=session.external_amount_msat,
        description=session.description,
        return_url=session.return_url,
        cancel_url=session.cancel_url,
        checkout_url=session.checkout_url,
        ledger_hold_id=str(session.ledger_hold_id)
        if session.ledger_hold_id
        else None,
        invoice_id=session.invoice_id,
        payment_request=session.payment_request,
        created_at=session.created_at.isoformat(),
        expires_at=session.expires_at.isoformat(),
    )


def _build_checkout_url(base_url: str, session_id: str) -> str:
    normalized_base_url = base_url.rstrip('/')
    return f'{normalized_base_url}/user/checkout?session_id={session_id}'
