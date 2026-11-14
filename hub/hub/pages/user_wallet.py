from hub.layout import (
    CARD_CLASS,
    MUTED_TEXT_CLASS,
    PANEL_TITLE_CLASS,
    SUBTLE_CARD_CLASS,
    USER_NAV_ITEMS,
    status_badge_classes,
)
from hub.ledger_client import LedgerHoldDetails, LedgerStatementEntry
from hub.models.tables import CheckoutSession
from hub.pages import user as _u
from hub.pages.user_components import (
    _fetch_wallet_snapshot,
    _format_msat,
    _format_optional_datetime,
    _require_user,
    _snapshot_signature,
    _summary_tile,
    _watch_wallet_snapshot,
)
from hub.wallet_service import WalletSnapshot


async def render_wallet_page(*, active_path: str) -> None:
    with _u.app_layout(
        title='Wallet',
        active_path=active_path,
        area='User',
        nav_items=USER_NAV_ITEMS,
    ):
        user_sub = _require_user()
        if user_sub is None:
            return

        state: dict[str, WalletSnapshot | None] = {
            'snapshot': await _fetch_wallet_snapshot(user_sub),
        }

        @_u.ui.refreshable
        def render_balances() -> None:
            _wallet_balance_cards(state['snapshot'])

        @_u.ui.refreshable
        def render_activity() -> None:
            _wallet_activity_sections(state['snapshot'])

        async def refresh_wallet_state(*, notify_error: bool = False) -> None:
            latest = await _fetch_wallet_snapshot(
                user_sub,
                notify_error=notify_error,
            )
            if latest is None:
                return
            if _snapshot_signature(latest) == _snapshot_signature(
                state['snapshot']
            ):
                return
            state['snapshot'] = latest
            render_balances.refresh()
            render_activity.refresh()

        render_balances()
        _wallet_action_bar()
        render_activity()
        _watch_wallet_snapshot(refresh_wallet_state)


def _wallet_action_bar() -> None:
    with _u.ui.row().classes('w-full items-center gap-3'):
        _u.ui.button(
            'Deposit',
            icon='south_west',
            on_click=lambda: _u.ui.navigate.to('/user/deposit'),
        ).props('unelevated no-caps color=primary').classes(
            'w-full sm:w-auto font-medium px-5'
        )
        _u.ui.button(
            'Withdraw',
            icon='north_east',
            on_click=lambda: _u.ui.navigate.to('/user/withdraw'),
        ).props('outline no-caps').classes('w-full sm:w-auto font-medium px-5')


def _wallet_balance_cards(snapshot: WalletSnapshot | None) -> None:
    balance_label = (
        _format_msat(snapshot.available_balance_msat)
        if snapshot is not None
        else 'Unavailable'
    )
    reserved_label = (
        _format_msat(snapshot.reserved_balance_msat)
        if snapshot is not None
        else 'Unavailable'
    )
    total_label = (
        _format_msat(snapshot.total_balance_msat)
        if snapshot is not None
        else 'Unavailable'
    )
    with _u.ui.grid().classes('w-full grid-cols-1 gap-4 md:grid-cols-3'):
        _summary_tile(
            'Available Balance',
            balance_label,
            'Ready to use',
            highlight=True,
        )
        _summary_tile('Pending Holds', reserved_label, 'Reserved funds')
        _summary_tile(
            'Total Balance',
            total_label,
            'Ledger source of truth',
        )


def _wallet_activity_sections(snapshot: WalletSnapshot | None) -> None:
    with _u.ui.grid().classes(
        'w-full grid-cols-1 gap-6 lg:grid-cols-2 items-start'
    ):
        _ledger_statement_panel(snapshot.statement_entries if snapshot else ())
        _recent_checkout_panel(snapshot.recent_checkouts if snapshot else ())

    if snapshot and snapshot.active_holds:
        _active_holds_panel(snapshot.active_holds)


def _recent_checkout_panel(
    recent_checkouts: tuple[CheckoutSession, ...],
) -> None:
    with _u.ui.card().classes(f'w-full {CARD_CLASS} gap-3'):
        _u.ui.label('Recent activity').classes(f'text-lg {PANEL_TITLE_CLASS}')
        if not recent_checkouts:
            _u.ui.label('No checkout activity yet.').classes(
                f'text-sm {MUTED_TEXT_CLASS}'
            )
            return
        for checkout_session in recent_checkouts:
            with _u.ui.column().classes(f'w-full {SUBTLE_CARD_CLASS} gap-1.5'):
                with _u.ui.row().classes(
                    'w-full items-center justify-between gap-2'
                ):
                    _u.ui.label(
                        f'{checkout_session.game_id} · '
                        f'#{checkout_session.order_id[:8]}'
                    ).classes('text-sm font-medium text-zinc-100')
                    _u.ui.label(checkout_session.status).classes(
                        status_badge_classes(checkout_session.status)
                    )
                with _u.ui.row().classes(
                    'w-full items-center justify-between flex-wrap gap-2'
                ):
                    _u.ui.label(
                        _format_msat(checkout_session.amount_msat)
                    ).classes(
                        'text-sm font-mono font-semibold '
                        'text-zinc-200 tabular-nums'
                    )
                    _u.ui.label(
                        f'Wallet: '
                        f'{_format_msat(checkout_session.internal_amount_msat)}'
                        f' · Lightning: '
                        f'{_format_msat(checkout_session.external_amount_msat)}'
                    ).classes(f'text-xs font-mono {MUTED_TEXT_CLASS}')


def _active_holds_panel(active_holds: tuple[LedgerHoldDetails, ...]) -> None:
    with _u.ui.card().classes(f'w-full {CARD_CLASS} gap-3'):
        _u.ui.label('Active holds').classes(f'text-lg {PANEL_TITLE_CLASS}')
        if not active_holds:
            _u.ui.label('No active holds in the ledger.').classes(
                f'text-sm {MUTED_TEXT_CLASS}'
            )
            return
        for hold in active_holds:
            with _u.ui.column().classes(f'w-full {SUBTLE_CARD_CLASS} gap-1'):
                with _u.ui.row().classes(
                    'w-full items-center justify-between gap-2'
                ):
                    _u.ui.label(
                        f'{_format_msat(hold.amount_msat)} | '
                        f'{hold.reason or "hold"}'
                    ).classes('text-sm font-medium text-zinc-100')
                    _u.ui.label(hold.status).classes(
                        status_badge_classes(hold.status)
                    )
                _u.ui.label(
                    ' | '.join([
                        f'Status: {hold.status}',
                        f'Reference: {hold.reference_type or "-"}',
                        (
                            'Expires: '
                            f'{_format_optional_datetime(hold.expires_at)}'
                        ),
                    ])
                ).classes(f'text-xs font-mono {MUTED_TEXT_CLASS}')


def _ledger_statement_panel(
    statement_entries: tuple[LedgerStatementEntry, ...],
) -> None:
    with _u.ui.card().classes(f'w-full {CARD_CLASS} gap-3'):
        _u.ui.label('Ledger statement').classes(f'text-lg {PANEL_TITLE_CLASS}')
        if not statement_entries:
            _u.ui.label('No ledger entries yet.').classes(
                f'text-sm {MUTED_TEXT_CLASS}'
            )
            return
        for entry in statement_entries:
            is_credit = entry.entry_type.lower() == 'credit'
            prefix = '+' if is_credit else '-'
            amount_cls = 'text-emerald-400' if is_credit else 'text-zinc-100'
            title = (
                entry.description
                or entry.reference_type.replace('_', ' ').title()
                or entry.entry_type.title()
            )
            subtitle = (
                entry.reference_type.replace('_', ' ')
                if entry.reference_type
                else 'ledger entry'
            )
            with _u.ui.row().classes(
                f'w-full {SUBTLE_CARD_CLASS} '
                'items-center justify-between gap-3'
            ):
                with _u.ui.column().classes('gap-0.5 min-w-0'):
                    _u.ui.label(title).classes(
                        'text-sm font-medium text-zinc-100'
                    )
                    _u.ui.label(subtitle).classes(
                        f'text-xs {MUTED_TEXT_CLASS} capitalize'
                    )
                with _u.ui.column().classes('items-end gap-1 shrink-0'):
                    _u.ui.label(
                        f'{prefix}{_format_msat(entry.amount_msat)}'
                    ).classes(
                        f'text-sm font-bold font-mono '
                        f'tabular-nums {amount_cls}'
                    )
                    _u.ui.label(entry.entry_type.title()).classes(
                        status_badge_classes(entry.entry_type)
                    )
