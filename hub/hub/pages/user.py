from nicegui import APIRouter, ui

from hub.auth import (
    current_roles,
    current_user,
    current_user_sub,
    keycloak_account_url,
)
from hub.checkout_service import (
    cancel_checkout_payment,
    get_or_create_user_ledger_account,
    prepare_checkout_payment,
    read_checkout,
)
from hub.layout import app_layout
from hub.ledger_client import LedgerClient
from hub.models.database import session_scope
from hub.pages.user_checkout import (
    _cancel_checkout,
    _checkout_available_balance,
    _checkout_composition,
    _checkout_content,
    _current_session_id,
    _load_checkout_session,
    _prepare_checkout_session_for_user,
    _watch_checkout_payment,
    render_checkout_page,
)
from hub.pages.user_components import (
    UserIdentity,
    _current_user_identity,
    _format_msat,
    _require_user,
)
from hub.pages.user_transfers import (
    DEPOSIT_METHODS,
    WITHDRAW_METHODS,
    _render_transfer_page,
)
from hub.pages.user_wallet import render_wallet_page
from hub.wallet_service import (
    WalletWithdrawalError,
    load_wallet_snapshot,
    request_deposit_invoice,
    withdraw_wallet_balance,
)

__all__ = (
    'DEPOSIT_METHODS',
    'WITHDRAW_METHODS',
    'LedgerClient',
    'UserIdentity',
    'WalletWithdrawalError',
    '_cancel_checkout',
    '_checkout_available_balance',
    '_checkout_composition',
    '_checkout_content',
    '_current_session_id',
    '_current_user_identity',
    '_format_msat',
    '_load_checkout_session',
    '_prepare_checkout_session_for_user',
    '_require_user',
    '_watch_checkout_payment',
    'app_layout',
    'cancel_checkout_payment',
    'checkout_page',
    'current_roles',
    'current_user',
    'current_user_sub',
    'deposit_page',
    'get_or_create_user_ledger_account',
    'keycloak_account_url',
    'load_wallet_snapshot',
    'prepare_checkout_payment',
    'profile_page',
    'read_checkout',
    'render_wallet_page',
    'request_deposit_invoice',
    'router',
    'session_scope',
    'ui',
    'wallet_page',
    'withdraw_page',
    'withdraw_wallet_balance',
)

router = APIRouter(prefix='/user')


@router.page('/wallet')
async def wallet_page() -> None:
    await render_wallet_page(active_path='/')


@router.page('/deposit')
async def deposit_page() -> None:
    await _render_transfer_page(mode='deposit')


@router.page('/withdraw')
async def withdraw_page() -> None:
    await _render_transfer_page(mode='withdraw')


@router.page('/checkout')
async def checkout_page() -> None:
    await render_checkout_page()


@router.page('/profile')
async def profile_page() -> None:
    ui.navigate.to(keycloak_account_url())
