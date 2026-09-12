from dataclasses import dataclass
from secrets import compare_digest

from fastapi import Request, status
from fastapi.responses import JSONResponse

from ledger.settings import get_settings

PUBLIC_PATHS = {'/', '/metrics'}


@dataclass(frozen=True)
class ServicePrincipal:
    name: str
    scopes: frozenset[str]


def _parse_internal_tokens(raw_tokens: str) -> dict[str, ServicePrincipal]:
    principals = {}
    for raw_token in raw_tokens.split(';'):
        token_config = raw_token.strip()
        if not token_config:
            continue

        service_name, separator, rest = token_config.partition(':')
        token, scopes_separator, raw_scopes = rest.partition(':')
        if (
            not separator
            or not scopes_separator
            or not service_name
            or not token
        ):
            continue

        scopes = frozenset(
            scope.strip() for scope in raw_scopes.split(',') if scope.strip()
        )
        principals[token] = ServicePrincipal(
            name=service_name,
            scopes=scopes or frozenset({'read'}),
        )
    return principals


def _extract_token(request: Request) -> str | None:
    internal_token = request.headers.get('X-Internal-Service-Token')
    if internal_token:
        return internal_token

    authorization = request.headers.get('Authorization')
    if authorization and authorization.startswith('Bearer '):
        return authorization.removeprefix('Bearer ').strip()

    return None


def _required_scope(request: Request) -> str:
    if request.method in {'GET', 'HEAD', 'OPTIONS'}:
        return 'read'
    return 'write'


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={'detail': {'code': code, 'message': message}},
    )


def authenticate_internal_request(
    request: Request,
) -> ServicePrincipal | JSONResponse:
    if request.url.path in PUBLIC_PATHS:
        return ServicePrincipal(name='public', scopes=frozenset({'read'}))

    principals_by_token = _parse_internal_tokens(
        get_settings().LEDGER_INTERNAL_API_TOKENS
    )
    if not principals_by_token:
        return ServicePrincipal(
            name='anonymous-dev',
            scopes=frozenset({'read', 'write', 'admin'}),
        )

    request_token = _extract_token(request)
    if not request_token:
        return _error_response(
            status.HTTP_401_UNAUTHORIZED,
            'missing_internal_token',
            'Internal service token is required',
        )

    principal = next(
        (
            principal
            for token, principal in principals_by_token.items()
            if compare_digest(token, request_token)
        ),
        None,
    )
    if principal is None:
        return _error_response(
            status.HTTP_401_UNAUTHORIZED,
            'invalid_internal_token',
            'Internal service token is invalid',
        )

    required_scope = _required_scope(request)
    if (
        'admin' not in principal.scopes
        and required_scope not in principal.scopes
    ):
        return _error_response(
            status.HTTP_403_FORBIDDEN,
            'insufficient_internal_scope',
            'Internal service token does not allow this operation',
        )

    return principal
