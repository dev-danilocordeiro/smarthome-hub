"""The webhook SSRF guard, on literal addresses (no DNS needed)."""

import pytest

from smarthome.modules.notifications.domain.errors import InvalidWebhook
from smarthome.modules.notifications.infrastructure.senders import AddressGuard


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/hook",
        "https://10.0.0.5/hook",
        "https://192.168.18.1/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/hook",
        "https://[fd00::1]/hook",
        "https://0.0.0.0/hook",
    ],
)
async def test_addresses_inside_the_network_are_refused_in_production(url: str) -> None:
    with pytest.raises(InvalidWebhook, match="non-public"):
        await AddressGuard(allow_private=False).check(url)


async def test_public_addresses_pass_and_development_allows_anything() -> None:
    await AddressGuard(allow_private=False).check("https://1.1.1.1/hook")
    await AddressGuard(allow_private=True).check("http://127.0.0.1:9000/hook")
