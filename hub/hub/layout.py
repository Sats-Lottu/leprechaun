from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from nicegui import ui

from hub.auth import current_user_sub, is_admin

BG_CLASS = 'bg-[#141821]'
HEADER_CLASS = 'bg-[#2c2a2b]'
CARD_CLASS = (
    'bg-[#2f3440] border border-gray-600 text-white shadow-none rounded-lg'
)
ACCENT_TEXT_CLASS = 'text-[#f5b400]'
MUTED_TEXT_CLASS = 'text-white/60'
VALUE_TEXT_CLASS = 'text-[#f5b400] font-bold'
PANEL_TITLE_CLASS = 'text-white font-semibold'
PROJECT_NAME = 'Leprechaun'


@dataclass(frozen=True)
class NavItem:
    label: str
    icon: str
    path: str


USER_NAV_ITEMS = (
    NavItem('Wallet', 'account_balance_wallet', '/user/wallet'),
    NavItem('Checkout', 'shopping_cart_checkout', '/user/checkout'),
    NavItem('Profile', 'account_circle', '/user/profile'),
)

ADMIN_NAV_ITEMS = (
    NavItem('Applications', 'apps', '/admin/applications'),
    NavItem('Users', 'group', '/admin/users'),
    NavItem('Balances', 'account_balance', '/admin/balances'),
    NavItem('Transactions', 'receipt_long', '/admin/transactions'),
    NavItem('Invoices', 'bolt', '/admin/invoices'),
    NavItem('Integrated Games', 'sports_esports', '/admin/games'),
    NavItem('Audit', 'policy', '/admin/audit'),
    NavItem('Settings', 'settings', '/admin/settings'),
    NavItem('Reports', 'monitoring', '/admin/reports'),
)


@contextmanager
def app_layout(
    *,
    title: str,
    active_path: str,
    area: str,
    nav_items: tuple[NavItem, ...],
) -> Iterator[None]:
    ui.colors(
        primary='#f5b400',
        secondary='#2f3440',
        accent='#37d67a',
        positive='#37d67a',
        negative='#ff5c5c',
        warning='#f5b400',
    )
    ui.query('body').classes(f'{BG_CLASS} text-white')
    ui.query('.nicegui-content').classes(f'{BG_CLASS} text-white')

    with ui.header().classes(
        f'{HEADER_CLASS} text-white border-b border-gray-700 px-3 md:px-6'
    ):
        with ui.row().classes('w-full items-center gap-3 no-wrap'):
            ui.button(
                icon='menu',
                on_click=lambda: drawer.toggle(),  # noqa: PLW0108
                color=None,
            ).props('flat round').classes('text-white')
            logo_placeholder(size='sm')
            with ui.column().classes('gap-0 min-w-0'):
                ui.label(PROJECT_NAME).classes(
                    f'text-sm font-semibold {ACCENT_TEXT_CLASS}'
                )
                ui.label(area).classes('text-xs text-white/60')
            ui.space()
            ui.button(
                'Logout',
                icon='logout',
                on_click=lambda: ui.navigate.to('/auth/logout'),
            ).props('flat').classes('hidden sm:flex text-white')

    with ui.left_drawer(value=False).classes(
        f'{BG_CLASS} text-white w-72 p-0 border-r border-gray-700'
    ) as drawer:
        with ui.column().classes('w-full min-h-full justify-between gap-0'):
            with ui.column().classes('w-full gap-0'):
                with ui.column().classes('px-5 py-5 gap-1'):
                    with ui.row().classes('items-center gap-2'):
                        logo_placeholder(size='md')
                        ui.label(PROJECT_NAME).classes(
                            f'text-lg font-bold {ACCENT_TEXT_CLASS}'
                        )
                    ui.label(_session_label()).classes('text-xs text-white/60')

                for item in nav_items:
                    _nav_link(item, active_path)

                if area != 'Admin' and is_admin():
                    ui.separator().classes('bg-gray-700 my-2')
                    _nav_link(
                        NavItem(
                            'Admin',
                            'admin_panel_settings',
                            '/admin/users',
                        ),
                        active_path,
                    )

            with ui.column().classes('w-full px-4 py-4 gap-2'):
                ui.separator().classes('bg-gray-700')
                ui.button(
                    'Sign out',
                    icon='logout',
                    on_click=lambda: ui.navigate.to('/auth/logout'),
                ).props('flat').classes('w-full justify-start text-white/80')

    with ui.column().classes(
        'w-full max-w-6xl mx-auto px-4 py-5 md:px-6 md:py-8 gap-5'
    ):
        ui.label(title).classes(
            f'text-2xl md:text-3xl font-bold {ACCENT_TEXT_CLASS}'
        )
        yield


def _nav_link(item: NavItem, active_path: str) -> None:
    active_classes = f'{HEADER_CLASS} text-white border-l-4 border-[#f5b400]'
    inactive_classes = 'text-white/70 hover:bg-[#2c2a2b] hover:text-white'
    state_classes = (
        active_classes if item.path == active_path else inactive_classes
    )
    ui.button(
        item.label,
        icon=item.icon,
        on_click=lambda path=item.path: ui.navigate.to(path),
    ).props('flat').classes(
        f'w-full justify-start rounded-none px-5 py-3 {state_classes}'
    )


def _session_label() -> str:
    return current_user_sub() or 'Anonymous session'


def logo_placeholder(*, size: str = 'md') -> None:
    size_class = 'w-7 h-7 text-sm' if size == 'sm' else 'w-9 h-9 text-base'
    ui.label('L').classes(
        f'{size_class} rounded-lg border border-[#f5b400] bg-[#2f3440] '
        f'{ACCENT_TEXT_CLASS} font-bold flex items-center justify-center'
    )
