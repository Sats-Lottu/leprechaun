from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from pls.models.tables import (
    LightningInvoice,
    OperationIdempotency,
    ProviderEvent,
)
from pls.provider import LNbitsWallet
from pls.schemas import Envelope


@dataclass(frozen=True, slots=True)
class CommandContext:
    env: Envelope
    lnbits_wallet: LNbitsWallet
    reply_to: str | None
    correlation_id: str
    session: AsyncSession | None = None
    idempotency: OperationIdempotency | None = None


@dataclass(frozen=True, slots=True)
class PaymentSentContext:
    command: CommandContext
    payment: Any
    user_id: str | None
    amount_msat: int | None


@dataclass(frozen=True, slots=True)
class InvoicePaidContext:
    session: AsyncSession
    invoice: LightningInvoice
    provider_event: ProviderEvent
    provider_invoice_id: str | None
    payment_hash: str | None
    amount_msat: int
    paid_at: datetime
