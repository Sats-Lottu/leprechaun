import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from authlib.integrations.starlette_client import OAuthError
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey
from starlette.requests import Request

from hub.auth import create_oauth
from hub.settings import Settings

ISSUER = 'http://localhost:8080/realms/leprechaun'
INTERNAL = 'http://keycloak:8080/realms/leprechaun'
CALLBACK = 'http://localhost:8000/auth/callback'
PLAYER = '00000000-0000-0000-0000-000000000300'


def metadata(backchannel: str = ISSUER) -> dict:
    return {
        'issuer': ISSUER,
        'authorization_endpoint': f'{ISSUER}/protocol/openid-connect/auth',
        'token_endpoint': f'{backchannel}/protocol/openid-connect/token',
        'userinfo_endpoint': f'{backchannel}/protocol/openid-connect/userinfo',
        'jwks_uri': f'{backchannel}/protocol/openid-connect/certs',
        'response_types_supported': ['code'],
        'id_token_signing_alg_values_supported': ['RS256'],
        'token_endpoint_auth_methods_supported': [
            'none',
            'client_secret_basic',
            'client_secret_post',
        ],
        'code_challenge_methods_supported': ['S256'],
    }


def client_for(transport, *, internal=INTERNAL, secret=None):
    client = create_oauth(
        Settings(
            OIDC_ISSUER=ISSUER,
            OIDC_SERVER_METADATA_URL=f'{INTERNAL}/.well-known/openid-configuration',
            OIDC_INTERNAL_BASE_URL=internal,
            OIDC_CLIENT_SECRET=secret,
        )
    ).create_client('oidc')
    client.client_kwargs['transport'] = transport
    return client


@pytest.mark.parametrize('internal', ['', INTERNAL])
async def test_discovery_keeps_public_issuer_and_authorization(internal):
    def provider(request):
        return httpx.Response(200, json=metadata())

    client = client_for(httpx.MockTransport(provider), internal=internal)
    for _ in range(2):
        discovered = await client.load_server_metadata()
        assert discovered['issuer'] == ISSUER
        assert discovered['authorization_endpoint'] == (
            f'{ISSUER}/protocol/openid-connect/auth'
        )
        base = internal or ISSUER
        assert discovered['token_endpoint'] == (
            f'{base}/protocol/openid-connect/token'
        )
        assert discovered['userinfo_endpoint'] == (
            f'{base}/protocol/openid-connect/userinfo'
        )
        assert (
            discovered['jwks_uri'] == f'{base}/protocol/openid-connect/certs'
        )


async def test_discovery_rejects_unexpected_issuer():
    def provider(request):
        return httpx.Response(200, json={**metadata(), 'issuer': INTERNAL})

    client = client_for(httpx.MockTransport(provider))
    with pytest.raises(OAuthError, match='invalid_issuer'):
        await client.load_server_metadata()


@pytest.mark.parametrize(
    'endpoint', ['token_endpoint', 'userinfo_endpoint', 'jwks_uri']
)
async def test_internal_routing_rejects_endpoints_outside_issuer(endpoint):
    def provider(request):
        return httpx.Response(
            200,
            json={
                **metadata(),
                endpoint: 'https://unexpected.example/endpoint',
            },
        )

    client = client_for(httpx.MockTransport(provider))
    with pytest.raises(OAuthError, match='invalid_provider_endpoint'):
        await client.load_server_metadata()


def request_for(session, query='') -> Request:
    return Request({
        'type': 'http',
        'method': 'GET',
        'scheme': 'http',
        'server': ('localhost', 8000),
        'path': '/auth/callback',
        'query_string': query.encode(),
        'headers': [],
        'session': session,
    })


@pytest.mark.parametrize('secret', [None, 'local-confidential-secret'])
@pytest.mark.parametrize('invalid_claim', [None, 'nonce', 'iss', 'aud'])
async def test_keycloak_code_flow_validates_signed_token(
    secret, invalid_claim
):
    key = RSAKey.generate_key(2048, parameters={'kid': 'keycloak-test'})
    authorization = {}
    requests = []

    def provider(request):
        requests.append(str(request.url))
        assert str(request.url).startswith(INTERNAL + '/')
        if request.url.path.endswith('/.well-known/openid-configuration'):
            return httpx.Response(200, json=metadata(backchannel=INTERNAL))
        if request.url.path.endswith('/protocol/openid-connect/certs'):
            return httpx.Response(200, json={'keys': [key.as_dict()]})
        assert str(request.url) == f'{INTERNAL}/protocol/openid-connect/token'
        form = parse_qs(request.content.decode())
        assert form['grant_type'] == ['authorization_code']
        assert form['code'] == ['keycloak-code']
        assert form['redirect_uri'] == [CALLBACK]
        challenge = (
            base64
            .urlsafe_b64encode(
                hashlib.sha256(form['code_verifier'][0].encode()).digest()
            )
            .rstrip(b'=')
            .decode()
        )
        assert authorization['code_challenge'] == [challenge]
        if secret:
            credentials = base64.b64encode(
                f'leprechaun-hub:{secret}'.encode()
            ).decode()
            assert request.headers['Authorization'] == f'Basic {credentials}'
        else:
            assert 'Authorization' not in request.headers
            assert form['client_id'] == ['leprechaun-hub']
        now = int(time.time())
        claims = {
            'iss': ISSUER,
            'sub': PLAYER,
            'aud': ['leprechaun-hub'],
            'iat': now,
            'exp': now + 300,
            'name': 'Local Player',
            'nonce': authorization['nonce'][0],
        }
        if invalid_claim:
            claims[invalid_claim] = 'invalid'
        signed = jwt.encode({'alg': 'RS256', 'kid': key.kid}, claims, key)
        return httpx.Response(
            200,
            json={
                'access_token': 'local-access-token',
                'token_type': 'Bearer',
                'expires_in': 300,
                'id_token': signed,
            },
        )

    client = client_for(
        httpx.MockTransport(provider), internal='', secret=secret
    )
    session = {}
    response = await client.authorize_redirect(request_for(session), CALLBACK)
    location = urlsplit(response.headers['location'])
    assert f'{location.scheme}://{location.netloc}{location.path}' == (
        f'{ISSUER}/protocol/openid-connect/auth'
    )
    authorization.update(parse_qs(location.query))
    assert authorization['scope'] == ['openid profile']
    assert authorization['code_challenge_method'] == ['S256']
    state = authorization['state'][0]
    callback = request_for(session, f'code=keycloak-code&state={state}')
    if invalid_claim:
        with pytest.raises(JoseError):
            await client.authorize_access_token(callback)
    else:
        token = await client.authorize_access_token(callback)
        assert token['userinfo']['sub'] == PLAYER
        assert token['userinfo']['iss'] == ISSUER
        assert f'{INTERNAL}/protocol/openid-connect/token' in requests
        assert f'{INTERNAL}/protocol/openid-connect/certs' in requests


async def test_oidc_callback_rejects_wrong_state():
    client = client_for(
        httpx.MockTransport(
            lambda request: httpx.Response(200, json=metadata())
        )
    )
    with pytest.raises(OAuthError, match='mismatching_state'):
        await client.authorize_access_token(
            request_for({}, 'code=keycloak-code&state=unknown')
        )
