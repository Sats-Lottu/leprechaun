from fastapi import HTTPException

from ledger.observability import LEDGER_ERRORS


def error_detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def ledger_error(status_code: int, code: str, message: str) -> HTTPException:
    LEDGER_ERRORS.labels(code=code).inc()
    return HTTPException(
        status_code=status_code,
        detail=error_detail(code, message),
    )
