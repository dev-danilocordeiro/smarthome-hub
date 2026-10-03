"""Sign a Keycloak dev user in through the BFF and write what k6 needs to act as them.

    apps/api/.venv/bin/python scripts/loadtest/session.py --user dave > .loadtest/session.json

Drives the real login (authorization code + PKCE through the Keycloak form), so the load
test exercises sessions exactly as browsers create them. Output: the session cookie,
the CSRF token, and the user's homes with their device ids.
"""

import argparse
import asyncio
import html
import json
import re
import sys
from http.cookies import SimpleCookie
from urllib.parse import urljoin

import httpx

FORM_ACTION = re.compile(r'id="kc-form-login"[^>]*action="([^"]+)"')
SESSION_COOKIE = "__Host-smarthome_session"


class Client:
    """httpx with a hand-kept cookie jar. httpx will not send `Secure` cookies (Keycloak's,
    and the BFF's `__Host-` session) over http://localhost, which browsers treat as a
    secure context; the dev stack is plain http."""

    def __init__(self) -> None:
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=False)
        self.jar: dict[str, str] = {}

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        headers = {"cookie": "; ".join(f"{k}={v}" for k, v in self.jar.items())} if self.jar else {}
        self.http.cookies.clear()
        response = await self.http.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        for raw in response.headers.get_list("set-cookie"):
            cookie: SimpleCookie = SimpleCookie()
            cookie.load(raw)
            for name, morsel in cookie.items():
                if morsel["max-age"] == "0" or "1970" in morsel["expires"]:
                    self.jar.pop(name, None)
                else:
                    self.jar[name] = morsel.value
        return response


async def sign_in(api: str, user: str, password: str) -> Client:
    client = Client()
    response = await client.request("GET", f"{api}/auth/login")
    response = await client.request("GET", response.headers["location"])
    action = FORM_ACTION.search(response.text)
    if not action:
        raise SystemExit("no Keycloak login form; is the stack up?")
    response = await client.request(
        "POST",
        html.unescape(action.group(1)),
        data={"username": user, "password": password, "credentialId": ""},
    )
    if "location" not in response.headers:
        raise SystemExit(f"login refused: {response.status_code}")
    response = await client.request("GET", urljoin(api, response.headers["location"]))
    if response.status_code != 200:  # noqa: PLR2004
        raise SystemExit(f"callback failed: {response.status_code} {response.text[:200]}")
    return client


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:5173/api")
    parser.add_argument("--user", default="dave")
    parser.add_argument("--password", default="smarthome-dev-1")
    args = parser.parse_args()

    client = await sign_in(args.api, args.user, args.password)
    try:
        session = (await client.request("GET", f"{args.api}/auth/session")).json()
        homes = []
        for home in (await client.request("GET", f"{args.api}/homes")).json():
            found = await client.request("GET", f"{args.api}/homes/{home['id']}/devices")
            homes.append({"id": home["id"], "devices": [d["id"] for d in found.json()]})
        cookie = client.jar[SESSION_COOKIE]
    finally:
        await client.http.aclose()
    output = {"cookie": f"{SESSION_COOKIE}={cookie}", "csrf": session["csrf_token"], "homes": homes}
    json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
