from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from nicegui import ui

from hub.auth import current_user_sub, is_admin, keycloak_account_url

BG_CLASS = 'bg-zinc-950'
HEADER_CLASS = 'bg-zinc-950/90 backdrop-blur-md'
CARD_CLASS = (
    'bg-zinc-900/90 border border-zinc-800/80 text-zinc-100 '
    'shadow-sm rounded-xl p-5 sm:p-6'
)
SUBTLE_CARD_CLASS = (
    'bg-zinc-950/60 border border-zinc-800/70 text-zinc-200 rounded-lg p-3.5'
)
ACCENT_TEXT_CLASS = 'text-amber-400'
MUTED_TEXT_CLASS = 'text-zinc-400'
VALUE_TEXT_CLASS = (
    'text-zinc-100 font-bold font-mono tracking-tight tabular-nums'
)
PANEL_TITLE_CLASS = 'text-zinc-100 font-semibold tracking-tight'
SECTION_LABEL_CLASS = (
    'text-xs font-semibold uppercase tracking-wider text-zinc-400'
)
PROJECT_NAME = 'Leprechaun'

_EMERALD_STATUSES = frozenset({
    'active',
    'settled',
    'credit',
    'paid',
    'enabled',
    'posted',
})
_AMBER_STATUSES = frozenset({
    'awaiting_payment',
    'awaiting_ledger',
    'reserved',
    'pending',
})
_ROSE_STATUSES = frozenset({
    'failed',
    'canceled',
    'disabled',
    'debit',
    'revoked',
})


def status_badge_classes(status: str) -> str:
    """Return semantic Tailwind badge classes for a financial status."""
    normalized = status.strip().lower()
    base = (
        'inline-flex items-center px-2 py-0.5 rounded-full '
        'text-xs font-medium border'
    )
    if normalized in _EMERALD_STATUSES:
        tone = 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
    elif normalized in _AMBER_STATUSES:
        tone = 'bg-amber-500/10 text-amber-400 border-amber-500/30'
    elif normalized in _ROSE_STATUSES:
        tone = 'bg-rose-500/10 text-rose-400 border-rose-500/30'
    else:
        tone = 'bg-zinc-800/80 text-zinc-300 border-zinc-700'
    return f'{base} {tone}'


@dataclass(frozen=True)
class NavItem:
    label: str
    icon: str
    path: str


USER_NAV_ITEMS = (
    NavItem('Wallet', 'account_balance_wallet', '/'),
    NavItem('Profile', 'account_circle', '/user/profile'),
)

ADMIN_NAV_ITEMS = (
    NavItem('Applications', 'apps', '/admin/applications'),
    NavItem('Balances', 'account_balance', '/admin/balances'),
    NavItem('Invoices', 'bolt', '/admin/invoices'),
    NavItem('Transactions', 'receipt_long', '/admin/transactions'),
    NavItem('Users', 'group', '/admin/users'),
    NavItem('Audit', 'policy', '/admin/audit'),
)


@contextmanager
def app_layout(
    *,
    title: str,
    active_path: str,
    area: str,
    nav_items: tuple[NavItem, ...],
) -> Iterator[None]:
    is_admin_area = area == 'Admin'
    ui.colors(
        primary='#f59e0b',
        secondary='#27272a',
        accent='#10b981',
        positive='#10b981',
        negative='#f43f5e',
        warning='#f59e0b',
    )
    ui.query('body').classes(
        f'body--dark {BG_CLASS} text-zinc-100 antialiased'
    )
    ui.query('.nicegui-content').classes(f'{BG_CLASS} text-zinc-100')

    with ui.header().classes(
        f'{HEADER_CLASS} text-zinc-100 '
        'border-b border-zinc-800/80 px-4 md:px-6'
    ):
        with ui.row().classes(
            'max-w-6xl mx-auto w-full items-center gap-3 no-wrap'
        ):
            if is_admin_area:
                _drawer_button(lambda: drawer.toggle())  # noqa: PLW0108
            logo_placeholder(size='sm')
            with ui.column().classes('gap-0 min-w-0'):
                ui.label(PROJECT_NAME).classes(
                    'text-sm font-bold tracking-tight text-zinc-100'
                )
                area_class = (
                    'text-xs text-amber-400/90 font-medium'
                    if is_admin_area
                    else 'text-xs text-zinc-400'
                )
                ui.label(area).classes(area_class)
            _desktop_nav(
                nav_items=nav_items,
                active_path=active_path,
                is_admin_area=is_admin_area,
            )
            ui.space()
            ui.label(_session_label()).classes(
                'hidden lg:flex text-xs font-mono text-zinc-400 '
                'bg-zinc-900 border border-zinc-800 rounded-md px-2.5 py-1'
            )
            ui.button(
                'Logout',
                icon='logout',
                on_click=lambda: ui.navigate.to('/auth/logout'),
            ).props('flat no-caps dense').classes(
                'hidden sm:flex text-zinc-300 hover:text-zinc-100'
            )
            if not is_admin_area:
                _drawer_button(lambda: drawer.toggle())  # noqa: PLW0108

    drawer_factory = ui.left_drawer if is_admin_area else ui.right_drawer
    border_class = 'border-r' if is_admin_area else 'border-l'
    with drawer_factory(value=False).classes(
        f'{BG_CLASS} text-zinc-100 w-72 p-0 {border_class} border-zinc-800'
    ) as drawer:
        _drawer_body(
            nav_items=nav_items,
            active_path=active_path,
            area=area,
            is_admin_area=is_admin_area,
        )

    with ui.column().classes(
        'w-full max-w-6xl mx-auto px-4 py-6 md:px-6 md:py-8 gap-6'
    ):
        ui.label(title).classes(
            'text-2xl md:text-3xl font-bold tracking-tight text-zinc-100'
        )
        yield


def _desktop_nav(
    *,
    nav_items: tuple[NavItem, ...],
    active_path: str,
    is_admin_area: bool,
) -> None:
    with ui.row().classes('hidden md:flex items-center gap-1 ml-4'):
        for item in nav_items:
            _header_nav_link(item, active_path)
        if not is_admin_area and is_admin():
            _header_nav_link(
                NavItem('Admin', 'admin_panel_settings', '/admin/users'),
                active_path,
            )
        elif is_admin_area:
            _header_nav_link(
                NavItem('Wallet', 'account_balance_wallet', '/'),
                active_path,
            )


def _resolve_nav_path(path: str) -> str:
    if path == '/user/profile':
        return keycloak_account_url()
    return path


def _header_nav_link(item: NavItem, active_path: str) -> None:
    is_active = item.path == active_path
    state_classes = (
        'bg-zinc-800/90 text-amber-400 font-medium'
        if is_active
        else 'text-zinc-400 hover:text-zinc-100 hover:bg-zinc-900'
    )
    target = _resolve_nav_path(item.path)
    ui.button(
        item.label,
        icon=item.icon,
        on_click=lambda path=target: ui.navigate.to(path),
    ).props('flat no-caps dense').classes(
        f'rounded-lg px-3 py-1.5 text-sm {state_classes}'
    )


def _drawer_body(
    *,
    nav_items: tuple[NavItem, ...],
    active_path: str,
    area: str,
    is_admin_area: bool,
) -> None:
    with ui.column().classes('w-full min-h-full justify-between gap-0'):
        with ui.column().classes('w-full gap-0'):
            with ui.column().classes('px-5 py-5 gap-1.5'):
                with ui.row().classes('items-center gap-2.5'):
                    logo_placeholder(size='md')
                    with ui.column().classes('gap-0'):
                        ui.label(PROJECT_NAME).classes(
                            'text-base font-bold tracking-tight text-zinc-100'
                        )
                        ui.label(area).classes('text-xs text-amber-400')
                ui.label(_session_label()).classes(
                    'text-xs font-mono text-zinc-400 break-all'
                )

            for item in nav_items:
                _nav_link(
                    item,
                    active_path,
                    right_aligned=not is_admin_area,
                )

            if area != 'Admin' and is_admin():
                ui.separator().classes('bg-zinc-800 my-2')
                _nav_link(
                    NavItem(
                        'Admin',
                        'admin_panel_settings',
                        '/admin/users',
                    ),
                    active_path,
                    right_aligned=True,
                )
            elif is_admin_area:
                ui.separator().classes('bg-zinc-800 my-2')
                _nav_link(
                    NavItem('Wallet', 'account_balance_wallet', '/'),
                    active_path,
                    right_aligned=False,
                )

        with ui.column().classes('w-full px-4 py-4 gap-2'):
            ui.separator().classes('bg-zinc-800')
            ui.button(
                'Sign out',
                icon='logout',
                on_click=lambda: ui.navigate.to('/auth/logout'),
            ).props('flat no-caps').classes(
                'w-full justify-start text-zinc-300 hover:text-zinc-100'
            )


def _drawer_button(on_click) -> None:
    ui.button(
        icon='menu',
        on_click=on_click,
        color=None,
    ).props('flat round').classes('text-zinc-300 hover:text-zinc-100')


def _nav_link(
    item: NavItem,
    active_path: str,
    *,
    right_aligned: bool = False,
) -> None:
    active_border = 'border-r-2' if right_aligned else 'border-l-2'
    active_classes = (
        f'bg-zinc-900 text-amber-400 font-medium '
        f'{active_border} border-amber-400'
    )
    inactive_classes = 'text-zinc-400 hover:bg-zinc-900/70 hover:text-zinc-100'
    state_classes = (
        active_classes if item.path == active_path else inactive_classes
    )
    target = _resolve_nav_path(item.path)
    ui.button(
        item.label,
        icon=item.icon,
        on_click=lambda path=target: ui.navigate.to(path),
    ).props('flat no-caps').classes(
        f'w-full justify-start rounded-none px-5 py-3 {state_classes}'
    )


def _session_label() -> str:
    return current_user_sub() or 'Anonymous session'


def logo_placeholder(*, size: str = 'md') -> None:
    size_class = 'w-8 h-8' if size == 'sm' else 'w-10 h-10'
    ui.image('/static/leprechaun-logo.png').props(
        'fit=contain no-spinner'
    ).classes(f'{size_class} shrink-0')
