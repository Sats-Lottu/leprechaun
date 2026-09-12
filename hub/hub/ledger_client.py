from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import httpx

from hub.identity import ledger_owner_id
from hub.settings import get_settings

HTTP_ERROR_MIN_STATUS = 400


@dataclass(frozen=True)
class LedgerBalance:
    balance_msat: int
    reserved_balance_msat: int
    available_balance_msat: int


@dataclass(frozen=True)
class LedgerAccount:
    account_id: UUID


@dataclass(frozen=True)
class LedgerHold:
    hold_id: UUID
    status: str


@dataclass(frozen=True)
class LedgerHoldDetails:
    hold_id: UUID
    account_id: UUID
    amount_msat: int
    status: str
    reason: str
    reference_type: str
    reference_id: UUID | None
    expires_at: datetime | None
    consumed_at: datetime | None
    released_at: datetime | None


@dataclass(frozen=True)
class LedgerHoldCreate:
    account_id: UUID
    amount_msat: int
    reason: str
    reference_id: UUID
    idempotency_key: str
    expires_at: datetime


class LedgerClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class LedgerStatementEntry:
    account_id: UUID
    entry_type: str
    amount_msat: int
    description: str
    reference_type: str
    reference_id: UUID | None


@dataclass(frozen=True)
class LedgerTransactionCreated:
    transaction_id: UUID
    status: str


@dataclass(frozen=True)
class LedgerTransactionEntryCreate:
    account_id: UUID
    entry_type: str
    amount_msat: int
    description: str = ''
    reference_type: str = ''
    reference_id: UUID | None = None


@dataclass(frozen=True)
class LedgerTransactionCreate:
    reference_type: str
    reference_id: UUID | None
    idempotency_key: str
    description: str
    entries: tuple[LedgerTransactionEntryCreate, ...]


class LedgerClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.LEDGER_BASE_URL).rstrip('/')
        self.token = (
            token if token is not None else settings.LEDGER_INTERNAL_API_TOKEN
        )
        self.timeout = timeout

    async def create_user_account(self, user_sub: str) -> LedgerAccount:
        payload = {
            'account_type': 'user',
            'owner_id': str(ledger_owner_id(user_sub)),
            'name': user_sub,
        }
        data = await self._request(
            'POST',
            '/accounts',
            params=payload,
        )
        return LedgerAccount(account_id=UUID(str(data['account_id'])))

    async def get_balance(self, account_id: UUID) -> LedgerBalance:
        data = await self._request('GET', f'/accounts/{account_id}/balance')
        return LedgerBalance(
            balance_msat=int(data['balance']),
            reserved_balance_msat=int(data['reserved_balance']),
            available_balance_msat=int(data['available_balance']),
        )

    async def create_hold(self, payload: LedgerHoldCreate) -> LedgerHold:
        data = await self._request(
            'POST',
            '/holds',
            json={
                'account_id': str(payload.account_id),
                'amount': payload.amount_msat,
                'reason': payload.reason,
                'reference_type': 'checkout_session',
                'reference_id': str(payload.reference_id),
                'idempotency_key': payload.idempotency_key,
                'expires_at': payload.expires_at.isoformat(),
            },
        )
        return LedgerHold(
            hold_id=UUID(str(data['hold_id'])),
            status=str(data['status']),
        )

    async def get_hold(self, hold_id: UUID) -> LedgerHoldDetails:
        data = await self._request('GET', f'/holds/{hold_id}')
        return _to_hold_details(data)

    async def list_holds(self, account_id: UUID) -> list[LedgerHoldDetails]:
        data = await self._request('GET', f'/holds/account/{account_id}')
        return [_to_hold_details(item) for item in data.get('holds', [])]

    async def release_hold(
        self,
        hold_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> LedgerHold:
        payload = {}
        if idempotency_key:
            payload['idempotency_key'] = idempotency_key
        data = await self._request(
            'POST',
            f'/holds/{hold_id}/release',
            json=payload,
        )
        return LedgerHold(
            hold_id=UUID(str(data['hold_id'])),
            status=str(data['status']),
        )

    async def get_statement(
        self, account_id: UUID
    ) -> list[LedgerStatementEntry]:
        data = await self._request('GET', f'/accounts/{account_id}/statement')
        return [
            LedgerStatementEntry(
                account_id=UUID(str(item['account_id'])),
                entry_type=str(item['entry_type']),
                amount_msat=int(item['amount']),
                description=str(item.get('description') or ''),
                reference_type=str(item.get('reference_type') or ''),
                reference_id=_optional_uuid(item.get('reference_id')),
            )
            for item in data.get('entries', [])
        ]

    async def create_transaction(
        self, payload: LedgerTransactionCreate
    ) -> LedgerTransactionCreated:
        data = await self._request(
            'POST',
            '/transactions',
            json={
                'reference_type': payload.reference_type,
                'reference_id': str(payload.reference_id)
                if payload.reference_id
                else None,
                'idempotency_key': payload.idempotency_key,
                'description': payload.description,
                'entries': [
                    {
                        'account_id': str(entry.account_id),
                        'entry_type': entry.entry_type,
                        'amount': entry.amount_msat,
                        'description': entry.description,
                        'reference_type': entry.reference_type,
                        'reference_id': str(entry.reference_id)
                        if entry.reference_id
                        else None,
                    }
                    for entry in payload.entries
                ],
            },
        )
        return LedgerTransactionCreated(
            transaction_id=UUID(str(data['transaction_id'])),
            status=str(data['status']),
        )

    async def post_transaction(
        self,
        transaction_id: UUID,
        *,
        idempotency_key: str | None = None,
    ) -> LedgerTransactionCreated:
        payload = {}
        if idempotency_key:
            payload['idempotency_key'] = idempotency_key
        data = await self._request(
            'POST',
            f'/transactions/{transaction_id}/post',
            json=payload,
        )
        return LedgerTransactionCreated(
            transaction_id=UUID(str(data['transaction_id'])),
            status=str(data['status']),
        )

    async def consume_hold(
        self,
        hold_id: UUID,
        *,
        transaction_id: UUID,
        idempotency_key: str | None = None,
    ) -> LedgerHold:
        payload = {'transaction_id': str(transaction_id)}
        if idempotency_key:
            payload['idempotency_key'] = idempotency_key
        data = await self._request(
            'POST',
            f'/holds/{hold_id}/consume',
            json=payload,
        )
        return LedgerHold(
            hold_id=UUID(str(data['hold_id'])),
            status=str(data['status']),
        )

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> dict:
        headers = dict(kwargs.pop('headers', {}) or {})
        if self.token:
            headers['X-Internal-Service-Token'] = self.token

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
        ) as client:
            response = await client.request(
                method,
                path,
                headers=headers,
                **kwargs,
            )

        if response.status_code >= HTTP_ERROR_MIN_STATUS:
            raise LedgerClientError(response.text)
        return response.json()


def _optional_datetime(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def _optional_uuid(value: object) -> UUID | None:
    if not value:
        return None
    return UUID(str(value))


def _to_hold_details(data: dict) -> LedgerHoldDetails:
    return LedgerHoldDetails(
        hold_id=UUID(str(data['hold_id'])),
        account_id=UUID(str(data['account_id'])),
        amount_msat=int(data['amount']),
        status=str(data['status']),
        reason=str(data.get('reason') or ''),
        reference_type=str(data.get('reference_type') or ''),
        reference_id=_optional_uuid(data.get('reference_id')),
        expires_at=_optional_datetime(data.get('expires_at')),
        consumed_at=_optional_datetime(data.get('consumed_at')),
        released_at=_optional_datetime(data.get('released_at')),
    )
