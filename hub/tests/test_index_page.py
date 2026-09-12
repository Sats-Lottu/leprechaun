from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hub.pages import index


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
        self.buttons: list[str] = []
        self.navigation: list[str] = []
        self.navigate = SimpleNamespace(to=self.navigation.append)

    def colors(self, **kwargs) -> None:
        self.colors_value = kwargs

    @staticmethod
    def query(selector: str) -> FakeElement:
        return FakeElement()

    @staticmethod
    def card() -> FakeElement:
        return FakeElement()

    @staticmethod
    def row() -> FakeElement:
        return FakeElement()

    @staticmethod
    def column() -> FakeElement:
        return FakeElement()

    @staticmethod
    def header() -> FakeElement:
        return FakeElement()

    @staticmethod
    def left_drawer(value: bool) -> FakeElement:
        return FakeElement()

    @staticmethod
    def icon(value: str) -> FakeElement:
        return FakeElement()

    @staticmethod
    def space() -> None:
        return None

    def label(self, value: str) -> FakeElement:
        self.labels.append(value)
        return FakeElement()

    def button(
        self,
        label: str,
        *,
        on_click,
        icon: str | None = None,
    ) -> FakeElement:
        self.buttons.append(label)
        on_click()
        return FakeElement()


@contextmanager
def fake_app_layout(**kwargs) -> Iterator[None]:
    yield


def fake_logo_placeholder(*, size: str = 'md') -> None:
    return None


@pytest.mark.asyncio
async def test_index_page_shows_login_for_anonymous_user(monkeypatch) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(index, 'ui', fake_ui)
    monkeypatch.setattr(index, 'app_layout', fake_app_layout)
    monkeypatch.setattr(index, 'logo_placeholder', fake_logo_placeholder)
    monkeypatch.setattr(index, 'current_user', lambda: None)

    await index.page()

    assert 'Leprechaun' in fake_ui.labels
    assert 'Hybrid checkout for Leprechaun games.' in fake_ui.labels
    assert 'Login' in fake_ui.buttons
    assert '/auth/login' in fake_ui.navigation


@pytest.mark.asyncio
async def test_index_page_shows_logout_for_authenticated_user(
    monkeypatch,
) -> None:
    fake_ui = FakeUi()
    monkeypatch.setattr(index, 'ui', fake_ui)
    monkeypatch.setattr(index, 'app_layout', fake_app_layout)
    monkeypatch.setattr(index, 'logo_placeholder', fake_logo_placeholder)
    monkeypatch.setattr(
        index,
        'current_user',
        lambda: {
            'sub': 'user-123',
            'name': 'Alice',
            'email': 'alice@example.com',
        },
    )
    monkeypatch.setattr(index, 'current_user_sub', lambda: 'user-123')
    monkeypatch.setattr(index, 'current_roles', lambda: {'admin'})

    await index.page()

    assert 'Leprechaun' in fake_ui.labels
    assert 'Welcome user-123!' in fake_ui.labels
    assert 'Signed in user' in fake_ui.labels
    assert (
        'Name: Alice | Email: alice@example.com | Roles: admin'
        in fake_ui.labels
    )
    assert 'Wallet' in fake_ui.buttons
    assert '/user/wallet' in fake_ui.navigation
