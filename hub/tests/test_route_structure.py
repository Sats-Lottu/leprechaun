from hub.pages.admin import router as admin_router
from hub.pages.user import router as user_router


def test_user_routes_are_grouped_under_user_prefix() -> None:
    paths = {route.path for route in user_router.routes}

    assert paths == {
        '/user/wallet',
        '/user/checkout',
        '/user/profile',
    }


def test_admin_routes_are_grouped_under_admin_prefix() -> None:
    paths = {route.path for route in admin_router.routes}

    assert paths == {
        '/admin/',
        '/admin/users',
        '/admin/balances',
        '/admin/transactions',
        '/admin/invoices',
        '/admin/games',
        '/admin/audit',
        '/admin/settings',
        '/admin/reports',
    }
