from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hub.models.enums import AdminRoleStatus, CheckoutSessionStatus
from hub.models.tables import AdminRoleAssignment, CheckoutSession

AMOUNT_MSAT = 1000


@pytest.mark.asyncio
async def test_admin_role_assignment_persists(session) -> None:
    role = AdminRoleAssignment(
        user_sub='admin-123',
        role='admin',
        granted_by='bootstrap',
    )
    session.add(role)
    await session.commit()

    stored_role = await session.scalar(
        select(AdminRoleAssignment).where(
            AdminRoleAssignment.user_sub == 'admin-123'
        )
    )

    assert stored_role is not None
    assert stored_role.role == 'admin'
    assert stored_role.status == AdminRoleStatus.ACTIVE


@pytest.mark.asyncio
async def test_admin_role_assignment_is_unique_by_user_and_role(
    session,
) -> None:
    session.add_all(
        [
            AdminRoleAssignment(user_sub='admin-123', role='admin'),
            AdminRoleAssignment(user_sub='admin-123', role='admin'),
        ]
    )

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_checkout_session_persists(session) -> None:
    checkout_session = CheckoutSession(
        user_id='user-123',
        game_id='leprechaun-game',
        order_id='order-1',
        amount_msat=AMOUNT_MSAT,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
        checkout_url='https://hub.example/user/checkout?session_id=1',
        expires_at=datetime(2026, 4, 20, 12, 15, tzinfo=timezone.utc),
    )
    session.add(checkout_session)
    await session.commit()

    stored_session = await session.scalar(
        select(CheckoutSession).where(CheckoutSession.order_id == 'order-1')
    )

    assert stored_session is not None
    assert stored_session.status == CheckoutSessionStatus.CREATED
    assert stored_session.amount_msat == AMOUNT_MSAT


@pytest.mark.asyncio
async def test_checkout_session_is_unique_by_game_and_order(session) -> None:
    common = {
        'game_id': 'leprechaun-game',
        'order_id': 'order-1',
        'amount_msat': AMOUNT_MSAT,
        'description': 'Ticket purchase',
        'return_url': 'https://game.example/return',
        'cancel_url': 'https://game.example/cancel',
        'checkout_url': 'https://hub.example/user/checkout?session_id=1',
        'expires_at': datetime(2026, 4, 20, 12, 15, tzinfo=timezone.utc),
    }
    session.add_all([CheckoutSession(**common), CheckoutSession(**common)])

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()


@pytest.mark.asyncio
async def test_checkout_session_amount_must_be_positive(session) -> None:
    checkout_session = CheckoutSession(
        game_id='leprechaun-game',
        order_id='order-1',
        amount_msat=0,
        description='Ticket purchase',
        return_url='https://game.example/return',
        cancel_url='https://game.example/cancel',
        checkout_url='https://hub.example/user/checkout?session_id=1',
        expires_at=datetime(2026, 4, 20, 12, 15, tzinfo=timezone.utc),
    )
    session.add(checkout_session)

    with pytest.raises(IntegrityError):
        await session.commit()

    await session.rollback()
