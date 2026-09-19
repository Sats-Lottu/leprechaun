from nicegui import APIRouter, ui
from sqlalchemy import func, select

from hub.auth import current_user_sub, is_admin
from hub.layout import (
    ADMIN_NAV_ITEMS,
    CARD_CLASS,
    MUTED_TEXT_CLASS,
    app_layout,
)
from hub.models.database import session_scope
from hub.models.enums import AdminRoleStatus
from hub.models.tables import AdminRoleAssignment, CheckoutSession
from hub.pages.admin_balances import render_admin_balances

router = APIRouter(prefix='/admin')


def _require_admin() -> bool:
    if current_user_sub() is None:
        ui.label('Login is required to access admin.').classes(
            'text-white/70 text-base'
        )
        ui.button(
            'Login',
            icon='login',
            on_click=lambda: ui.navigate.to('/auth/login'),
        ).classes('w-full sm:w-auto')
        return False

    if not is_admin():
        ui.label('Admin role is required.').classes('text-[#ff5c5c] text-base')
        ui.button(
            'Go to wallet',
            icon='account_balance_wallet',
            on_click=lambda: ui.navigate.to('/'),
        ).props('outline').classes('w-full sm:w-auto')
        return False

    return True


@router.page('/')
async def admin_home_page() -> None:
    ui.navigate.to('/admin/users')


@router.page('/users')
async def users_page() -> None:
    await _admin_page('Users', '/admin/users', 'Manage payment users.')


@router.page('/balances')
async def balances_page() -> None:
    with app_layout(
        title='Balances',
        active_path='/admin/balances',
        area='Admin',
        nav_items=ADMIN_NAV_ITEMS,
    ):
        if not _require_admin():
            return
        await render_admin_balances()


@router.page('/transactions')
async def transactions_page() -> None:
    await _admin_page(
        'Transactions',
        '/admin/transactions',
        'Inspect ledger transaction history.',
    )


@router.page('/invoices')
async def invoices_page() -> None:
    await _admin_page(
        'Invoices',
        '/admin/invoices',
        'Track payment invoices.',
    )


@router.page('/games')
async def games_page() -> None:
    await _admin_page(
        'Integrated Games',
        '/admin/games',
        'Configure game checkout integrations.',
    )


@router.page('/audit')
async def audit_page() -> None:
    await _admin_page(
        'Audit',
        '/admin/audit',
        'Review operational audit records.',
    )


@router.page('/settings')
async def settings_page() -> None:
    await _admin_page(
        'Settings',
        '/admin/settings',
        'Configure payment UI options.',
    )


@router.page('/reports')
async def reports_page() -> None:
    await _admin_page(
        'Reports',
        '/admin/reports',
        'Monitor operational reports.',
    )


async def _admin_page(title: str, active_path: str, description: str) -> None:
    with app_layout(
        title=title,
        active_path=active_path,
        area='Admin',
        nav_items=ADMIN_NAV_ITEMS,
    ):
        if not _require_admin():
            return

        with ui.card().classes(f'w-full {CARD_CLASS}'):
            ui.label(description).classes('text-white/80')
            await _admin_data(title)


async def _admin_data(title: str) -> None:
    if title == 'Users':
        async with session_scope() as session:
            roles = await session.scalars(
                select(AdminRoleAssignment)
                .where(AdminRoleAssignment.status == AdminRoleStatus.ACTIVE)
                .order_by(AdminRoleAssignment.user_sub)
            )
            rows = [
                {
                    'user_sub': role.user_sub,
                    'role': role.role,
                    'granted_by': role.granted_by,
                }
                for role in roles
            ]
        _table(
            rows,
            [
                {'name': 'user_sub', 'label': 'User', 'field': 'user_sub'},
                {'name': 'role', 'label': 'Role', 'field': 'role'},
                {
                    'name': 'granted_by',
                    'label': 'Granted by',
                    'field': 'granted_by',
                },
            ],
        )
        return

    async with session_scope() as session:
        checkout_sessions = await session.scalars(
            select(CheckoutSession)
            .order_by(CheckoutSession.created_at.desc())
            .limit(20)
        )
        rows = [
            {
                'game_id': item.game_id,
                'order_id': item.order_id,
                'status': item.status,
                'amount': _format_msat(item.amount_msat),
                'internal': _format_msat(item.internal_amount_msat),
                'external': _format_msat(item.external_amount_msat),
                'invoice_id': item.invoice_id or '-',
            }
            for item in checkout_sessions
        ]

        totals = await session.execute(
            select(
                CheckoutSession.status,
                func.count(CheckoutSession.id),
            ).group_by(CheckoutSession.status)
        )
        summary = ', '.join(
            f'{status}: {count}' for status, count in totals.all()
        )

    ui.label(summary or 'No checkout sessions yet.').classes(
        f'text-sm {MUTED_TEXT_CLASS}'
    )
    _table(
        rows,
        [
            {'name': 'game_id', 'label': 'Game', 'field': 'game_id'},
            {'name': 'order_id', 'label': 'Order', 'field': 'order_id'},
            {'name': 'status', 'label': 'Status', 'field': 'status'},
            {'name': 'amount', 'label': 'Amount', 'field': 'amount'},
            {'name': 'internal', 'label': 'Internal', 'field': 'internal'},
            {'name': 'external', 'label': 'External', 'field': 'external'},
            {
                'name': 'invoice_id',
                'label': 'Invoice',
                'field': 'invoice_id',
            },
        ],
    )


def _table(rows: list[dict], columns: list[dict]) -> None:
    if not rows:
        ui.label('No persisted records yet.').classes(
            f'text-sm {MUTED_TEXT_CLASS}'
        )
        return
    ui.table(columns=columns, rows=rows).classes('w-full').props('flat dense')


def _format_msat(amount_msat: int) -> str:
    return f'{amount_msat // 1000} sats'
