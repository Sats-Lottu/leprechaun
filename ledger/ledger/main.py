from fastapi import FastAPI

from ledger.auth import ServicePrincipal, authenticate_internal_request
from ledger.observability import (
    configure_logging,
    metrics_response,
    record_http_metrics,
)
from ledger.routes import account_router, holds_router, transactions_router

configure_logging()

app = FastAPI()

app.include_router(account_router)
app.include_router(holds_router)
app.include_router(transactions_router)


@app.middleware("http")
async def internal_auth_middleware(request, call_next):
    principal = authenticate_internal_request(request)
    if isinstance(principal, ServicePrincipal):
        request.state.service_principal = principal
        return await call_next(request)
    return principal


@app.middleware("http")
async def observability_middleware(request, call_next):
    return await record_http_metrics(request, call_next)


@app.get("/")
async def root():
    return {"message": "Ledger service"}


@app.get("/metrics")
async def metrics():
    return metrics_response()
