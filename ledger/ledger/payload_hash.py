import json
from hashlib import sha256
from typing import Any

from pydantic import BaseModel


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode='json'))
    if isinstance(value, dict):
        return {
            str(key): _normalize(item)
            for key, item in sorted(
                value.items(), key=lambda pair: str(pair[0])
            )
        }
    if isinstance(value, list | tuple):
        return [_normalize(item) for item in value]
    return value


def payload_hash(payload: Any) -> str:
    normalized = _normalize(payload)
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode()
    return sha256(encoded).hexdigest()
