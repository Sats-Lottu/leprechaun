from dataclasses import dataclass

import segno

from hub.layout import (
    ACCENT_TEXT_CLASS,
    CARD_CLASS,
    MUTED_TEXT_CLASS,
    PANEL_TITLE_CLASS,
    SECTION_LABEL_CLASS,
    VALUE_TEXT_CLASS,
    status_badge_classes,
)
from hub.ledger_client import LedgerClientError
from hub.pages import user as _u
from hub.wallet_service import WalletSnapshot


@dataclass(frozen=True)
class UserIdentity:
    subject: str
    display_name: str
    preferred_username: str
    email: str
    roles: tuple[str, ...]


def _require_user() -> str | None:
    user_sub = _u.current_user_sub()
    if user_sub is None:
        _u.ui.label('Login is required to continue.').classes(
            'text-zinc-300 text-base'
        )
        _u.ui.button(
            'Login',
            icon='login',
            on_click=lambda: _u.ui.navigate.to('/auth/login'),
        ).props('unelevated no-caps color=primary').classes(
            'w-full sm:w-auto font-medium'
        )
        return None
    return user_sub


def _current_user_identity() -> UserIdentity:
    user_info = _u.current_user() or {}
    user_sub = _u.current_user_sub() or str(user_info.get('sub') or 'unknown')
    display_name = (
        str(user_info.get('name') or '')
        or str(user_info.get('preferred_username') or '')
        or user_sub
    )
    preferred_username = str(
        user_info.get('preferred_username') or user_info.get('nickname') or '-'
    )
    email = str(user_info.get('email') or '-')
    return UserIdentity(
        subject=user_sub,
        display_name=display_name,
        preferred_username=preferred_username,
        email=email,
        roles=tuple(sorted(_u.current_roles())),
    )


def _format_msat(amount_msat: int) -> str:
    return f'{amount_msat // 1000:,} sats'


def _format_optional_datetime(value) -> str:
    return value.isoformat() if value else '-'


def _summary_tile(
    title: str,
    value: str,
    description: str,
    *,
    highlight: bool = False,
) -> None:
    accent = 'border-amber-500/30' if highlight else ''
    with _u.ui.card().classes(f'w-full {CARD_CLASS} {accent} gap-1.5'):
        _u.ui.label(title).classes(SECTION_LABEL_CLASS)
        _u.ui.label(value).classes(
            f'text-2xl sm:text-3xl {VALUE_TEXT_CLASS} break-all'
        )
        _u.ui.label(description).classes(f'text-xs {MUTED_TEXT_CLASS}')


def _copy_invoice(payment_request: str) -> None:
    _u.ui.clipboard.write(payment_request)
    _u.ui.notify('Invoice copied to clipboard!', type='positive')


def _payment_request_qr(payment_request: str) -> None:
    qrcode = segno.make_qr(payment_request, error='l')
    with _u.ui.link(target=f'lightning:{payment_request}').tooltip(
        'Open in Lightning Wallet'
    ):
        _u.ui.image(qrcode.svg_data_uri(light='white', border=1)).classes(
            'w-60 max-w-full bg-white rounded-xl p-3 shadow-md'
        ).tooltip('Scan with your Lightning Wallet')
    _u.ui.label(payment_request).classes(
        'mt-3 w-full break-all font-mono text-xs text-center '
        'text-zinc-300 bg-zinc-950/80 border border-zinc-800 '
        'rounded-lg p-2.5 cursor-pointer hover:border-amber-500/50'
    ).on(
        'click',
        lambda _e: _copy_invoice(payment_request),
    ).tooltip('Click to copy')


def _qr_code_dialog(
    *,
    title: str,
    amount_label: str,
    payment_request: str,
    subtitle: str = '',
):
    with _u.ui.dialog() as dialog:
        with _u.ui.card().classes(
            f'w-full max-w-md mx-auto {CARD_CLASS} '
            'items-center text-center gap-4'
        ):
            with _u.ui.row().classes(
                'w-full items-center justify-between gap-2'
            ):
                _u.ui.label(title).classes(f'text-lg {PANEL_TITLE_CLASS}')
                _u.ui.label('awaiting payment').classes(
                    status_badge_classes('awaiting_payment')
                )
            _u.ui.label(amount_label).classes(
                f'text-3xl font-bold font-mono '
                f'{ACCENT_TEXT_CLASS} tabular-nums'
            )
            if subtitle:
                _u.ui.label(subtitle).classes(f'text-xs {MUTED_TEXT_CLASS}')
            _payment_request_qr(payment_request)
            with _u.ui.row().classes(
                'w-full items-center justify-end gap-2 pt-2'
            ):
                _u.ui.button(
                    'Copy Invoice',
                    icon='content_copy',
                    on_click=lambda: _copy_invoice(payment_request),
                ).props('outline no-caps dense')
                _u.ui.button(
                    'Close',
                    icon='close',
                    on_click=dialog.close,
                ).props('flat no-caps dense').classes(
                    'text-zinc-400 hover:text-zinc-100'
                )
    return dialog


async def _fetch_wallet_snapshot(
    user_sub: str,
    *,
    notify_error: bool = True,
) -> WalletSnapshot | None:
    try:
        async with _u.session_scope() as session:
            return await _u.load_wallet_snapshot(
                user_sub=user_sub,
                session=session,
                ledger=_u.LedgerClient(),
            )
    except LedgerClientError:
        if notify_error:
            _u.ui.notify('Ledger is unavailable.', type='warning')
        return None


def _snapshot_signature(snapshot: WalletSnapshot | None) -> tuple | None:
    if snapshot is None:
        return None
    return (
        snapshot.available_balance_msat,
        snapshot.reserved_balance_msat,
        snapshot.total_balance_msat,
        snapshot.open_invoice_count,
        tuple(
            (
                entry.entry_type,
                entry.amount_msat,
                entry.description,
                entry.reference_type,
                entry.reference_id,
            )
            for entry in snapshot.statement_entries
        ),
        tuple(
            (hold.hold_id, hold.amount_msat, hold.status, hold.expires_at)
            for hold in snapshot.active_holds
        ),
        tuple(
            (
                item.game_id,
                item.order_id,
                item.status,
                item.amount_msat,
                item.internal_amount_msat,
                item.external_amount_msat,
            )
            for item in snapshot.recent_checkouts
        ),
    )


def _watch_wallet_snapshot(
    refresh_callback,
    *,
    interval: float = 3.0,
) -> None:
    in_flight = False

    async def poll_wallet() -> None:
        nonlocal in_flight
        if in_flight:
            return
        if _u.current_user_sub() is None:
            timer.cancel()
            return
        in_flight = True
        try:
            await refresh_callback()
        finally:
            in_flight = False

    timer = _u.ui.timer(interval, poll_wallet)
