from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from ledger.models.enums import (
    AccountType,
    EntryType,
    HoldStatus,
    TransactionKind,
    TransactionStatus,
)


class APIModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        use_enum_values=True,
    )


class CreateAccountParams(BaseModel):
    account_type: AccountType = AccountType.USER
    owner_id: UUID | None = None
    name: str | None = None


class AccountCreated(BaseModel):
    account_id: UUID
    account_type: AccountType
    owner_id: UUID | None = None
    name: str | None = None


class AccountDetails(BaseModel):
    account_id: UUID
    account_type: AccountType
    owner_id: UUID | None = None
    name: str = ''
    balance: int
    reserved_balance: int
    available_balance: int
    is_active: bool


class AccountsList(BaseModel):
    accounts: list[AccountDetails] = Field(default_factory=list)


class AccountBalance(BaseModel):
    balance: int
    reserved_balance: int

    @computed_field
    @property
    def available_balance(self) -> int:
        return max(self.balance - self.reserved_balance, 0)


class AccountStatement(BaseModel):
    entries: list[dict] = Field(default_factory=list)


class HoldCreate(BaseModel):
    account_id: UUID
    amount: int
    reason: str = ""
    reference_type: str = ""
    reference_id: UUID | None = None
    idempotency_key: str | None = None
    expires_at: datetime | None = None


class HoldCreated(BaseModel):
    hold_id: UUID
    status: HoldStatus


class HoldDetails(BaseModel):
    hold_id: UUID
    account_id: UUID
    amount: int
    status: HoldStatus
    reason: str = ""
    reference_type: str = ""
    reference_id: UUID | None = None
    expires_at: datetime | None = None
    consumed_at: datetime | None = None
    released_at: datetime | None = None


class AccountHolds(BaseModel):
    account_id: UUID
    holds: list[HoldDetails] = Field(default_factory=list)


class HoldStatusResponse(BaseModel):
    hold_id: UUID
    status: HoldStatus


class HoldOperation(BaseModel):
    idempotency_key: str | None = None
    transaction_id: UUID | None = None


class ExpireHoldsResponse(BaseModel):
    expired_count: int


class TransactionCreated(BaseModel):
    transaction_id: UUID
    status: TransactionStatus


class TransactionEntryCreate(BaseModel):
    account_id: UUID
    entry_type: EntryType
    amount: int
    description: str = ""
    reference_type: str = ""
    reference_id: UUID | None = None


class TransactionCreate(BaseModel):
    kind: TransactionKind = TransactionKind.TRANSFER
    external_origin: str = Field(default='', max_length=50)
    reference_type: str = ""
    reference_id: UUID | None = None
    idempotency_key: str | None = None
    description: str = ""
    entries: list[TransactionEntryCreate] = Field(default_factory=list)


class TransactionEntryDetails(BaseModel):
    account_id: UUID
    entry_type: EntryType
    amount: int
    description: str = ""
    reference_type: str = ""
    reference_id: UUID | None = None


class TransactionDetails(BaseModel):
    transaction_id: UUID
    status: TransactionStatus
    kind: TransactionKind
    external_origin: str = ''
    reference_type: str = ""
    reference_id: UUID | None = None
    description: str = ""
    reversed_transaction_id: UUID | None = None
    entries: list[TransactionEntryDetails] = Field(default_factory=list)


class TransactionOperation(BaseModel):
    idempotency_key: str | None = None
