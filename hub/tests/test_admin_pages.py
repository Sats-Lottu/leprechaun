from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hub.models.enums import CheckoutSessionStatus
from hub.models.tables import AdminRoleAssignment, CheckoutSession
from hub.pages import admin

AMOUNT_MSAT = 12_000


class FakeElement:
    def __enter__(self) -> 'FakeElement':
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def classes(self, value: str) -> 'FakeElement':
        self.classes_value = value
        return self

    def props(self, value: str) -> 'FakeElement':
        self.props_value = value
        return self


class FakeUi:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.tables: list[dict] = []
        self.navigation: list[str] = []
        self.navigate = SimpleNamespace(to=self.navigation.append)

    def label(self, value: str) -> FakeElement:
        self.labels.append(value)
        return FakeElement()

    def table(self, *, columns: list[dict], rows: list[dict]) -> FakeElement:
        self.tables.append({'columns': columns, 'rows': rows})
        return FakeElement()

    @staticmethod
    def card() -> FakeElement:
        return FakeElement()


@pytest.mark.asyncio
async def test_admin_home_redirects_to_users(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(admin, 'ui', fake_ui)

    await admin.admin_home_page()

    assert fake_ui.navigation == ['/admin/users']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('page', 'title', 'active_path', 'description'),
    [
        (admin.users_page, 'Users', '/admin/users', 'Manage payment users.'),
        (
            admin.balances_page,
            'Balances',
            '/admin/balances',
            'Review ledger balances.',
        ),
        (
            admin.transactions_page,
            'Transactions',
            '/admin/transactions',
            'Inspect ledger transaction history.',
        ),
        (
            admin.invoices_page,
            'Invoices',
            '/admin/invoices',
            'Track payment invoices.',
        ),
        (
            admin.games_page,
            'Integrated Games',
            '/admin/games',
            'Configure game checkout integrations.',
        ),
        (
            admin.audit_page,
            'Audit',
            '/admin/audit',
            'Review operational audit records.',
        ),
        (
            admin.settings_page,
            'Settings',
            '/admin/settings',
            'Configure payment UI options.',
        ),
        (
            admin.reports_page,
            'Reports',
            '/admin/reports',
            'Monitor operational reports.',
        ),
    ],
)
async def test_admin_routes_delegate_to_admin_page(
    monkeypatch,
    page,
    title: str,
    active_path: str,
    description: str,
) -> None:
    admin_page = AsyncMock()
    monkeypatch.setattr(admin, '_admin_page', admin_page)

    await page()

    admin_page.assert_awaited_once_with(title, active_path, description)


@pytest.mark.asyncio
async def test_admin_data_lists_active_internal_roles(
    session,
    monkeypatch,
) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(admin, 'ui', fake_ui)
    session.add_all(
        [
            AdminRoleAssignment(
                user_sub='admin-123',
                role='admin',
                granted_by='bootstrap',
            ),
            AdminRoleAssignment(
                user_sub='admin-123',
                role='viewer',
                status='revoked',
            ),
        ]
    )
    await session.commit()

    await admin._admin_data('Users')  # noqa: SLF001

    assert fake_ui.tables == [
        {
            'columns': [
                {'name': 'user_sub', 'label': 'User', 'field': 'user_sub'},
                {'name': 'role', 'label': 'Role', 'field': 'role'},
                {
                    'name': 'granted_by',
                    'label': 'Granted by',
                    'field': 'granted_by',
                },
            ],
            'rows': [
                {
                    'user_sub': 'admin-123',
                    'role': 'admin',
                    'granted_by': 'bootstrap',
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_admin_data_lists_checkout_summary(
    session,
    monkeypatch,
) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(admin, 'ui', fake_ui)
    session.add(
        CheckoutSession(
            game_id='leprechaun-game',
            order_id='order-1',
            amount_msat=AMOUNT_MSAT,
            internal_amount_msat=4_000,
            external_amount_msat=8_000,
            invoice_id='invoice-1',
            status=CheckoutSessionStatus.AWAITING_PAYMENT,
            description='Ticket purchase',
            return_url='https://game.example/return',
            cancel_url='https://game.example/cancel',
            checkout_url='https://hub.example/user/checkout?session_id=1',
            expires_at=datetime(2026, 4, 20, 12, 15, tzinfo=timezone.utc),
        )
    )
    await session.commit()

    await admin._admin_data('Invoices')  # noqa: SLF001

    assert 'awaiting_payment: 1' in fake_ui.labels
    assert fake_ui.tables[0]['rows'] == [
        {
            'game_id': 'leprechaun-game',
            'order_id': 'order-1',
            'status': 'awaiting_payment',
            'amount': '12 sats',
            'internal': '4 sats',
            'external': '8 sats',
            'invoice_id': 'invoice-1',
        }
    ]


def test_admin_table_renders_empty_state(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(admin, 'ui', fake_ui)

    admin._table([], [])  # noqa: SLF001

    assert fake_ui.labels == ['No persisted records yet.']
    assert fake_ui.tables == []


def test_admin_format_msat() -> None:
    assert admin._format_msat(42_000) == '42 sats'  # noqa: SLF001
