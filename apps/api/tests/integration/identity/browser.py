"""Just enough of a browser to drive an OIDC login: one cookie jar per host (browsers
ignore ports for cookies), manual redirects, and the Keycloak login form."""

import html
import re
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

import httpx

FORM_ACTION = re.compile(r'id="kc-form-login"[^>]*action="([^"]+)"')
META_REFRESH = re.compile(r'http-equiv="refresh" content="0;url=([^"]+)"')


class Browser:
    def __init__(self, *, app: httpx.AsyncClient, network: httpx.AsyncClient, app_url: str) -> None:
        self._app = app
        self._network = network
        self._app_url = app_url
        self.cookies: dict[str, dict[str, str]] = {}  # host -> name -> value
        self.raw_set_cookies: list[str] = []
        self.bodies: list[str] = []

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        jar = self.cookies.setdefault(host, {})
        headers = dict(kwargs.pop("headers", {}) or {})  # type: ignore[call-overload]
        if jar:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        client = self._app if url.startswith(self._app_url) else self._network
        # httpx keeps its own jar per client; clients are shared between Browser instances,
        # so only this object's jar may decide what is sent.
        client.cookies.clear()
        response = await client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        for raw in response.headers.get_list("set-cookie"):
            self.raw_set_cookies.append(raw)
            parsed: SimpleCookie = SimpleCookie()
            parsed.load(raw)
            for name, morsel in parsed.items():
                if morsel["max-age"] == "0" or "1970" in morsel["expires"]:
                    jar.pop(name, None)
                else:
                    jar[name] = morsel.value
        self.bodies.append(response.text)
        return response

    def cookie(self, name: str) -> str | None:
        return next((jar[name] for jar in self.cookies.values() if name in jar), None)

    async def start_login(self, query: str = "") -> httpx.Response:
        """Hit /auth/login and follow to the IdP. Returns the IdP's response."""
        response = await self.request("GET", f"{self._app_url}/auth/login{query}")
        assert response.status_code == 302, response.text
        return await self.request("GET", response.headers["location"])

    async def submit_credentials(self, login_page: httpx.Response, username: str) -> str:
        """Fill the Keycloak form; returns the callback URL Keycloak redirects to."""
        match = FORM_ACTION.search(login_page.text)
        assert match, "no login form on the page"
        response = await self.request(
            "POST",
            html.unescape(match.group(1)),
            data={"username": username, "password": "smarthome-dev-1", "credentialId": ""},
        )
        assert response.status_code == 302, response.text[:500]
        return response.headers["location"]

    async def finish(self, callback_url: str) -> str:
        """Deliver the callback; returns where the page navigates next."""
        response = await self.request("GET", callback_url)
        assert response.status_code == 200, response.text
        match = META_REFRESH.search(response.text)
        assert match
        return html.unescape(match.group(1))

    async def login(self, username: str = "alice", query: str = "") -> str:
        page = await self.start_login(query)
        return await self.finish(await self.submit_credentials(page, username))
