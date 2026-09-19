from types import SimpleNamespace

from hub.pages import admin, user


class FakeElement:
    def classes(self, value: str) -> 'FakeElement':
        self.classes_value = value
        return self

    def props(self, value: str) -> 'FakeElement':
        self.props_value = value
        return self


class FakeUi:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.buttons: list[str] = []
        self.navigation: list[str] = []
        self.navigate = SimpleNamespace(to=self.navigation.append)

    def label(self, value: str) -> FakeElement:
        self.labels.append(value)
        return FakeElement()

    def button(self, label: str, *, icon: str, on_click) -> FakeElement:
        self.buttons.append(label)
        on_click()
        return FakeElement()


def test_user_guard_prompts_login_when_user_is_missing(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(user, 'ui', fake_ui)
    monkeypatch.setattr(user, 'current_user_sub', lambda: None)

    assert user._require_user() is None  # noqa: SLF001
    assert 'Login is required to continue.' in fake_ui.labels
    assert 'Login' in fake_ui.buttons
    assert '/auth/login' in fake_ui.navigation


def test_user_guard_returns_user_sub(monkeypatch) -> None:
    monkeypatch.setattr(user, 'current_user_sub', lambda: 'user-123')

    assert user._require_user() == 'user-123'  # noqa: SLF001


def test_admin_guard_prompts_login_when_user_is_missing(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(admin, 'ui', fake_ui)
    monkeypatch.setattr(admin, 'current_user_sub', lambda: None)

    assert not admin._require_admin()  # noqa: SLF001
    assert 'Login is required to access admin.' in fake_ui.labels
    assert 'Login' in fake_ui.buttons
    assert '/auth/login' in fake_ui.navigation


def test_admin_guard_rejects_user_without_admin_role(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(admin, 'ui', fake_ui)
    monkeypatch.setattr(admin, 'current_user_sub', lambda: 'user-123')
    monkeypatch.setattr(admin, 'is_admin', lambda: False)

    assert not admin._require_admin()  # noqa: SLF001
    assert 'Admin role is required.' in fake_ui.labels
    assert 'Go to wallet' in fake_ui.buttons
    assert '/' in fake_ui.navigation


def test_admin_guard_accepts_admin(monkeypatch) -> None:
    monkeypatch.setattr(admin, 'current_user_sub', lambda: 'admin-123')
    monkeypatch.setattr(admin, 'is_admin', lambda: True)

    assert admin._require_admin()  # noqa: SLF001
