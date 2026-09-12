import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

from pls.schemas import Failed
from pls.settings import get_settings

settings = get_settings()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expires_at_iso(expires_in_sec: int | None) -> str:
    if not expires_in_sec:
        return now_utc_iso()
    return (
        datetime.now(timezone.utc) + timedelta(seconds=int(expires_in_sec))
    ).isoformat()


def msat_to_sat_floor(amount_msat: int) -> int:
    return max(0, int(amount_msat) // 1000)


def iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def payload_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def is_transient_failure(result: Failed) -> bool:
    return result.detail in {"Timeout!", "Connect error!"}


async def retry_provider_call(call, *args):
    attempts = max(1, settings.COMMAND_RETRY_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        result = await call(*args)
        if not isinstance(result, Failed) or not is_transient_failure(result):
            return result
        if attempt < attempts:
            await asyncio.sleep(settings.COMMAND_RETRY_BACKOFF_SEC * attempt)
    return result
