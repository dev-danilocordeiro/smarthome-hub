"""Minimal OpenID Connect relying party for the BFF (authorization code + PKCE).

Kept small on purpose: discovery, the authorize URL, code exchange, refresh, ID token
validation against the provider's JWKS and the end-session URL. Everything a library
would hide is visible here and covered by tests.
"""

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from smarthome.shared.clock import Clock

ALLOWED_ALGORITHMS = ("RS256", "PS256", "ES256")
CLOCK_LEEWAY = timedelta(seconds=30)
JWKS_MIN_REFRESH_INTERVAL = 60.0


class OidcError(Exception):
    """The provider answered something we cannot accept, or could not be reached."""


class ProviderUnavailable(OidcError):
    pass


class RefreshRejected(OidcError):
    """The refresh token is no longer valid: rotated already, revoked, or the IdP session ended."""


class InvalidIdToken(OidcError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    access_expires_at: datetime
    refresh_token: str | None
    refresh_expires_at: datetime | None
    id_token: str | None


@dataclass(frozen=True, slots=True)
class IdTokenClaims:
    subject: str
    email: str | None
    name: str | None
    authenticated_at: datetime
    issued_at: datetime


@dataclass(frozen=True, slots=True)
class Pkce:
    verifier: str
    challenge: str

    @staticmethod
    def generate() -> "Pkce":
        verifier = secrets.token_urlsafe(64)  # 86 chars, inside RFC 7636's 43-128 range
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return Pkce(verifier=verifier, challenge=challenge)


class OidcClient:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        discovery_url: str,
        client_id: str,
        client_secret: str,
        clock: Clock,
    ) -> None:
        self._http = http
        self._discovery_url = discovery_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = clock
        self._metadata: ProviderMetadata | None = None
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_fetched_at = float("-inf")
        self._lock = asyncio.Lock()

    @property
    def client_id(self) -> str:
        return self._client_id

    async def metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            async with self._lock:
                if self._metadata is None:
                    doc = await self._get_json(self._discovery_url)
                    self._metadata = ProviderMetadata(
                        issuer=doc["issuer"],
                        authorization_endpoint=doc["authorization_endpoint"],
                        token_endpoint=doc["token_endpoint"],
                        jwks_uri=doc["jwks_uri"],
                        end_session_endpoint=doc.get("end_session_endpoint"),
                    )
        return self._metadata

    async def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        nonce: str,
        code_challenge: str,
        force_reauthentication: bool = False,
    ) -> str:
        meta = await self.metadata()
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if force_reauthentication:
            # Step-up: make the user prove presence again even with a live IdP session.
            params |= {"prompt": "login", "max_age": "0"}
        return f"{meta.authorization_endpoint}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> TokenSet:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def validate_id_token(self, id_token: str, *, nonce: str | None) -> IdTokenClaims:
        meta = await self.metadata()
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise InvalidIdToken(f"malformed ID token: {exc}") from exc
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise InvalidIdToken(f"unexpected signing algorithm {header.get('alg')!r}")

        key = await self._signing_key(header.get("kid"))
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key=key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self._client_id,
                issuer=meta.issuer,
                leeway=CLOCK_LEEWAY,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidIdToken(str(exc)) from exc

        if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise InvalidIdToken("nonce mismatch")
        if "azp" in claims and claims["azp"] != self._client_id:
            raise InvalidIdToken("token was issued to another client")

        issued_at = datetime.fromtimestamp(claims["iat"], UTC)
        auth_time = claims.get("auth_time")
        return IdTokenClaims(
            subject=claims["sub"],
            email=claims.get("email"),
            name=claims.get("name") or claims.get("preferred_username"),
            authenticated_at=datetime.fromtimestamp(auth_time, UTC) if auth_time else issued_at,
            issued_at=issued_at,
        )

    async def end_session_url(
        self, *, id_token_hint: str | None, post_logout_redirect_uri: str
    ) -> str:
        meta = await self.metadata()
        if meta.end_session_endpoint is None:
            return post_logout_redirect_uri
        params = {
            "client_id": self._client_id,
            "post_logout_redirect_uri": post_logout_redirect_uri,
        }
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        return f"{meta.end_session_endpoint}?{urlencode(params)}"

    async def _token_request(self, form: dict[str, str]) -> TokenSet:
        meta = await self.metadata()
        try:
            response = await self._http.post(
                meta.token_endpoint,
                data=form,
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"token endpoint unreachable: {type(exc).__name__}") from exc

        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise ProviderUnavailable(f"token endpoint returned {response.status_code}")
        body: dict[str, Any] = response.json()
        if response.status_code != httpx.codes.OK:
            error = body.get("error", "unknown_error")
            if form["grant_type"] == "refresh_token" and error == "invalid_grant":
                raise RefreshRejected(body.get("error_description", error))
            raise OidcError(f"token request failed: {error}")

        now = self._clock.now()
        refresh_expires_in = body.get("refresh_expires_in")
        return TokenSet(
            access_token=body["access_token"],
            access_expires_at=now + timedelta(seconds=int(body.get("expires_in", 60))),
            refresh_token=body.get("refresh_token"),
            # Keycloak reports 0 for offline tokens, meaning "no fixed expiry".
            refresh_expires_at=(
                now + timedelta(seconds=int(refresh_expires_in)) if refresh_expires_in else None
            ),
            id_token=body.get("id_token"),
        )

    async def _signing_key(self, kid: str | None) -> jwt.PyJWK:
        if self._jwks is None:
            await self._load_jwks()
        key = self._find_key(kid)
        if key is None and time.monotonic() - self._jwks_fetched_at > JWKS_MIN_REFRESH_INTERVAL:
            # Unknown kid: the provider probably rotated keys. Refetch, but rate-limited so a
            # flood of forged tokens cannot turn us into a JWKS amplifier.
            await self._load_jwks()
            key = self._find_key(kid)
        if key is None:
            raise InvalidIdToken(f"no signing key with kid {kid!r}")
        return key

    def _find_key(self, kid: str | None) -> jwt.PyJWK | None:
        if self._jwks is None:
            return None
        # Providers also publish encryption keys (Keycloak: RSA-OAEP); only signing keys count.
        signing = [k for k in self._jwks.keys if k.public_key_use in (None, "sig")]
        if kid is None:
            return signing[0] if len(signing) == 1 else None
        return next((k for k in signing if k.key_id == kid), None)

    async def _load_jwks(self) -> None:
        meta = await self.metadata()
        self._jwks = jwt.PyJWKSet.from_dict(await self._get_json(meta.jwks_uri))
        self._jwks_fetched_at = time.monotonic()

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = await self._http.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{url} unreachable: {type(exc).__name__}") from exc
        result: dict[str, Any] = response.json()
        return result
