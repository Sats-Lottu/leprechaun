from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from ledger.auth import (
    PUBLIC_PATHS,
    ServicePrincipal,
    authenticate_internal_request,
)
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


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )
    schema.setdefault('components', {}).setdefault('securitySchemes', {})[
        'InternalServiceToken'
    ] = {
        'type': 'apiKey',
        'in': 'header',
        'name': 'X-Internal-Service-Token',
        'description': 'Token used for service-to-service Ledger API calls.',
    }
    for path, path_definition in schema['paths'].items():
        if path in PUBLIC_PATHS:
            continue
        for operation in path_definition.values():
            if isinstance(operation, dict) and 'responses' in operation:
                operation['security'] = [{'InternalServiceToken': []}]

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


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
