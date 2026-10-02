import httpx
import pytest
from fastapi import FastAPI

from smarthome.modules.identity.api.bff import safe_return_to
from smarthome.shared.http.security_headers import SecurityHeadersMiddleware


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/",
        "//evil.example/path",
        "/\\evil.example",
        "javascript:alert(1)",
        "/ok\r\nSet-Cookie: x=1",
        "/" + "a" * 600,
        "",
    ],
)
def test_return_targets_that_could_leave_the_site_are_replaced_by_the_root(value: str) -> None:
    assert safe_return_to(value) == "/"


@pytest.mark.parametrize("value", ["/", "/homes/123?tab=devices", "/a/b#c"])
def test_plain_relative_paths_are_kept(value: str) -> None:
    assert safe_return_to(value) == value


async def test_every_response_carries_the_security_headers_and_is_not_cached() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/thing")
    def thing() -> dict[str, str]:
        return {"ok": "yes"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/thing")

    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "max-age" in response.headers["strict-transport-security"]
