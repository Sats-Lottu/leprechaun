from hub import layout


class FakeElement:
    def __enter__(self) -> 'FakeElement':
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def classes(self, value: str) -> 'FakeElement':
        return self

    def props(self, value: str) -> 'FakeElement':
        return self

    @staticmethod
    def toggle() -> None:
        return None


class FakeUi:
    def __init__(self) -> None:
        self.left_drawer_calls = 0
        self.right_drawer_calls = 0

    @staticmethod
    def colors(**kwargs) -> None:
        return None

    @staticmethod
    def query(selector: str) -> FakeElement:
        return FakeElement()

    @staticmethod
    def header() -> FakeElement:
        return FakeElement()

    @staticmethod
    def row() -> FakeElement:
        return FakeElement()

    @staticmethod
    def column() -> FakeElement:
        return FakeElement()

    @staticmethod
    def label(value: str) -> FakeElement:
        return FakeElement()

    @staticmethod
    def image(source: str) -> FakeElement:
        return FakeElement()

    @staticmethod
    def button(*args, **kwargs) -> FakeElement:
        return FakeElement()

    @staticmethod
    def separator() -> FakeElement:
        return FakeElement()

    @staticmethod
    def space() -> None:
        return None

    def left_drawer(self, *, value: bool) -> FakeElement:
        self.left_drawer_calls += 1
        return FakeElement()

    def right_drawer(self, *, value: bool) -> FakeElement:
        self.right_drawer_calls += 1
        return FakeElement()


def test_user_layout_places_navigation_on_the_right(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(layout, 'ui', fake_ui)
    monkeypatch.setattr(layout, 'is_admin', lambda: False)
    monkeypatch.setattr(layout, 'current_user_sub', lambda: 'user-123')

    with layout.app_layout(
        title='Wallet',
        active_path='/',
        area='User',
        nav_items=layout.USER_NAV_ITEMS,
    ):
        pass

    assert fake_ui.left_drawer_calls == 0
    assert fake_ui.right_drawer_calls == 1


def test_admin_layout_keeps_navigation_on_the_left(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(layout, 'ui', fake_ui)
    monkeypatch.setattr(layout, 'is_admin', lambda: True)
    monkeypatch.setattr(layout, 'current_user_sub', lambda: 'admin-123')

    with layout.app_layout(
        title='Users',
        active_path='/admin/users',
        area='Admin',
        nav_items=layout.ADMIN_NAV_ITEMS,
    ):
        pass

    assert fake_ui.left_drawer_calls == 1
    assert fake_ui.right_drawer_calls == 0
