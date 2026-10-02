import base64
import hashlib
import json
import time
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from smarthome.modules.identity.infrastructure.oidc import (
    InvalidIdToken,
    OidcClient,
    OidcError,
    Pkce,
    ProviderUnavailable,
    RefreshRejected,
)
from tests.unit.identity.fakes import FakeClock

ISSUER = "https://idp.test/realms/smarthome"
CLIENT_ID = "smarthome-bff"


class FakeProvider:
    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}
        self.jwks_fetches = 0
        self.token_response: tuple[int, dict[str, Any]] = (200, {})
        self.token_requests: list[dict[str, list[str]]] = []
        self.add_key("k1")

    def add_key(self, kid: str) -> None:
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def sign(self, kid: str = "k1", **overrides: Any) -> str:
        # PyJWT checks exp/iat against the wall clock, so tokens use real time.
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-1",
            "iat": now,
            "exp": now + 300,
            "auth_time": now - 10,
            "nonce": "n-1",
            "email": "alice@example.com",
            "name": "Alice",
        } | overrides
        return jwt.encode(claims, self.keys[kid], algorithm="RS256", headers={"kid": kid})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/certs",
                    "end_session_endpoint": f"{ISSUER}/logout",
                },
            )
        if path.endswith("/certs"):
            self.jwks_fetches += 1
            keys = []
            for kid, key in self.keys.items():
                jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
                keys.append(jwk | {"kid": kid, "alg": "RS256", "use": "sig"})
            # Keycloak also publishes an encryption key; it must be ignored.
            enc = RSAAlgorithm.to_jwk(self.keys["k1"].public_key(), as_dict=True)
            keys.append(enc | {"kid": "enc", "alg": "RSA-OAEP", "use": "enc"})
            return httpx.Response(200, json={"keys": keys})
        if path.endswith("/token"):
            self.token_requests.append(parse_qs(request.content.decode()))
            status, body = self.token_response
            return httpx.Response(status, json=body)
        return httpx.Response(404)


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(provider: FakeProvider) -> OidcClient:
    return OidcClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)),
        discovery_url=f"{ISSUER}/.well-known/openid-configuration",
        client_id=CLIENT_ID,
        client_secret="s3cret",
        clock=FakeClock(),
    )


def test_the_pkce_challenge_is_the_s256_of_the_verifier() -> None:
    pkce = Pkce.generate()

    expected = base64.urlsafe_b64encode(hashlib.sha256(pkce.verifier.encode()).digest())
    assert pkce.challenge == expected.rstrip(b"=").decode()
    assert 43 <= len(pkce.verifier) <= 128


async def test_the_authorization_url_asks_for_code_with_s256_pkce(client: OidcClient) -> None:
    url = await client.authorization_url(
        redirect_uri="https://app/cb", state="st", nonce="nn", code_challenge="cc"
    )

    query = parse_qs(urlsplit(url).query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid profile email"]
    assert "prompt" not in query


async def test_reauthentication_forces_the_login_prompt(client: OidcClient) -> None:
    url = await client.authorization_url(
        redirect_uri="https://app/cb",
        state="st",
        nonce="nn",
        code_challenge="cc",
        force_reauthentication=True,
    )

    query = parse_qs(urlsplit(url).query)
    assert query["prompt"] == ["login"]
    assert query["max_age"] == ["0"]


async def test_a_valid_id_token_yields_the_user_and_when_they_authenticated(
    client: OidcClient, provider: FakeProvider
) -> None:
    claims = await client.validate_id_token(provider.sign(), nonce="n-1")

    assert claims.subject == "user-1"
    assert claims.email == "alice@example.com"
    assert claims.authenticated_at < claims.issued_at


@pytest.mark.parametrize(
    ("overrides", "nonce"),
    [
        pytest.param({"aud": "someone-else"}, "n-1", id="wrong-audience"),
        pytest.param({"iss": "https://evil.test"}, "n-1", id="wrong-issuer"),
        pytest.param({"nonce": "replayed"}, "n-1", id="wrong-nonce"),
        pytest.param({"exp": 1_000}, "n-1", id="expired"),
        pytest.param({"azp": "someone-else"}, "n-1", id="issued-to-another-client"),
    ],
)
async def test_id_tokens_that_are_not_for_us_are_rejected(
    client: OidcClient, provider: FakeProvider, overrides: dict[str, Any], nonce: str
) -> None:
    with pytest.raises(InvalidIdToken):
        await client.validate_id_token(provider.sign(**overrides), nonce=nonce)


async def test_a_token_signed_with_an_unknown_key_is_rejected(client: OidcClient) -> None:
    forger = FakeProvider()  # its own keys, same kid

    with pytest.raises(InvalidIdToken):
        await client.validate_id_token(forger.sign(), nonce="n-1")


async def test_an_hmac_token_is_rejected_even_if_signed_with_the_public_key(
    client: OidcClient, provider: FakeProvider
) -> None:
    # Classic algorithm-confusion attack: HS256 keyed with the provider's public key.
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "kid": "k1"}).encode())
    body = base64.urlsafe_b64encode(json.dumps({"sub": "admin"}).encode())
    forged = f"{header.decode().rstrip('=')}.{body.decode().rstrip('=')}.c2ln"

    with pytest.raises(InvalidIdToken, match="algorithm"):
        await client.validate_id_token(forged, nonce=None)


async def test_a_rotated_signing_key_is_picked_up_once_the_cache_is_old_enough(
    client: OidcClient, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    await client.validate_id_token(provider.sign(), nonce="n-1")
    provider.add_key("k2")
    token = provider.sign("k2")

    with pytest.raises(InvalidIdToken):  # within the refetch rate limit
        await client.validate_id_token(token, nonce="n-1")

    monkeypatch.setattr(client, "_jwks_fetched_at", 0.0)
    claims = await client.validate_id_token(token, nonce="n-1")
    assert claims.subject == "user-1"
    assert provider.jwks_fetches == 2


async def test_the_code_exchange_sends_the_verifier_and_computes_expiries(
    client: OidcClient, provider: FakeProvider
) -> None:
    provider.token_response = (
        200,
        {
            "access_token": "at",
            "expires_in": 300,
            "refresh_token": "rt",
            "refresh_expires_in": 1800,
            "id_token": "it",
        },
    )

    tokens = await client.exchange_code(code="c", code_verifier="v", redirect_uri="https://app/cb")

    sent = provider.token_requests[-1]
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code_verifier"] == ["v"]
    assert tokens.access_expires_at - FakeClock().now() == timedelta(seconds=300)
    assert tokens.refresh_expires_at is not None


ErrorCase = tuple[int, dict[str, Any], type[OidcError]]


@pytest.mark.parametrize(
    "case",
    [
        pytest.param((400, {"error": "invalid_grant"}, RefreshRejected), id="reused-or-revoked"),
        pytest.param((503, {"error": "down"}, ProviderUnavailable), id="provider-down"),
        pytest.param((401, {"error": "invalid_client"}, OidcError), id="misconfigured-client"),
    ],
)
async def test_refresh_failures_are_classified_so_the_session_layer_can_react(
    client: OidcClient, provider: FakeProvider, case: ErrorCase
) -> None:
    status, body, expected = case
    provider.token_response = (status, body)

    with pytest.raises(expected):
        await client.refresh("rt")


async def test_an_unreachable_provider_is_reported_as_unavailable(provider: FakeProvider) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    broken = OidcClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(boom)),
        discovery_url=f"{ISSUER}/.well-known/openid-configuration",
        client_id=CLIENT_ID,
        client_secret="s",
        clock=FakeClock(),
    )
    with pytest.raises(ProviderUnavailable):
        await broken.refresh("rt")
