"""Manages device credentials and ACLs in Mosquitto's dynamic-security plugin.

Each device gets its own broker client (username = client id = device id) and a
dedicated role whose ACL covers only that device's topics. Everything else is denied
by the plugin's defaults. Commands go over MQTT to `$CONTROL/dynamic-security/v1`;
each one carries `correlationData` so its response can be matched.
"""

import asyncio
import contextlib
import json
import secrets
import ssl
from dataclasses import dataclass
from typing import Any

import aiomqtt
import structlog
from aiomqtt import ProtocolVersion

from device_protocol import device_publishes, device_subscribes

log = structlog.get_logger(__name__)

CONTROL_TOPIC = "$CONTROL/dynamic-security/v1"
RESPONSE_TOPIC = f"{CONTROL_TOPIC}/response"
RESPONSE_TIMEOUT = 5.0


class BrokerAdminError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BrokerConnection:
    host: str
    port: int
    ca_file: str
    username: str
    password: str

    def tls_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=self.ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context


def device_role(device_id: str) -> str:
    return f"device:{device_id}"


def device_acls(home_id: str, device_id: str) -> list[dict[str, Any]]:
    publish = [
        {"acltype": "publishClientSend", "topic": t, "allow": True}
        for t in device_publishes(home_id, device_id)
    ]
    subscribe = [
        {"acltype": "subscribeLiteral", "topic": t, "allow": True}
        for t in device_subscribes(home_id, device_id)
    ]
    return publish + subscribe


class BrokerAdmin:
    def __init__(self, connection: BrokerConnection) -> None:
        self._conn = connection

    async def register_device(self, *, home_id: str, device_id: str, password: str) -> None:
        """Create the device's client and role. Fails if the device already exists."""
        role = device_role(device_id)
        await self._execute(
            [
                {
                    "command": "createRole",
                    "rolename": role,
                    "acls": device_acls(home_id, device_id),
                },
                {
                    "command": "createClient",
                    "username": device_id,
                    # Pinning the client id stops one device from taking over another's
                    # MQTT session (and its queued commands) with its own credentials.
                    "clientid": device_id,
                    "password": password,
                    "textname": f"device in home {home_id}",
                    "roles": [{"rolename": role}],
                },
            ]
        )

    async def disable_device(self, device_id: str) -> None:
        """Reject new connections and disconnect the device right away (quarantine)."""
        await self._execute([{"command": "disableClient", "username": device_id}])

    async def enable_device(self, device_id: str) -> None:
        await self._execute([{"command": "enableClient", "username": device_id}])

    async def remove_device(self, device_id: str) -> None:
        """Revoke: delete the credentials; a connected device is kicked immediately."""
        await self._execute(
            [
                {"command": "deleteClient", "username": device_id},
                {"command": "deleteRole", "rolename": device_role(device_id)},
            ]
        )

    async def create_service_account(
        self, *, username: str, password: str, acls: list[dict[str, Any]]
    ) -> None:
        role = f"service:{username}"
        await self._execute(
            [
                {"command": "createRole", "rolename": role, "acls": acls},
                {
                    "command": "createClient",
                    "username": username,
                    "password": password,
                    "roles": [{"rolename": role}],
                },
            ]
        )

    async def ensure_service_account(
        self, *, username: str, password: str, acls: list[dict[str, Any]]
    ) -> None:
        """Create the account, or bring an existing one to exactly these ACLs and password
        (so deploying a new ACL set or rotating the secret only takes a restart)."""
        try:
            await self.create_service_account(username=username, password=password, acls=acls)
        except BrokerAdminError as exc:
            if "already exists" not in str(exc):
                raise
            role = f"service:{username}"
            current = await self._execute([{"command": "getRole", "rolename": role}])
            existing = current[0].get("data", {}).get("role", {}).get("acls", [])
            await self._execute(
                [
                    *(
                        {
                            "command": "removeRoleACL",
                            "rolename": role,
                            "acltype": a["acltype"],
                            "topic": a["topic"],
                        }
                        for a in existing
                    ),
                    *({"command": "addRoleACL", "rolename": role, **acl} for acl in acls),
                    {"command": "setClientPassword", "username": username, "password": password},
                ]
            )
            # Re-link client and role. Mosquitto 2.1 keeps a dangling link if the role was
            # ever deleted under the client, and then refuses addClientRole until the link
            # is removed; removing a link that is fine is harmless.
            with contextlib.suppress(BrokerAdminError):
                await self._execute(
                    [{"command": "removeClientRole", "username": username, "rolename": role}]
                )
            await self._execute(
                [{"command": "addClientRole", "username": username, "rolename": role}]
            )

    async def _execute(self, commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tagged = [c | {"correlationData": secrets.token_hex(8)} for c in commands]
        expected = {c["correlationData"]: c["command"] for c in tagged}
        responses: dict[str, dict[str, Any]] = {}

        async with aiomqtt.Client(
            self._conn.host,
            self._conn.port,
            username=self._conn.username,
            password=self._conn.password,
            protocol=ProtocolVersion.V5,
            tls_context=self._conn.tls_context(),
            identifier=f"hub-admin-{secrets.token_hex(4)}",
        ) as client:
            await client.subscribe(RESPONSE_TOPIC, qos=1)
            await client.publish(CONTROL_TOPIC, json.dumps({"commands": tagged}), qos=1)
            try:
                async with asyncio.timeout(RESPONSE_TIMEOUT):
                    async for message in client.messages:
                        body = json.loads(bytes(message.payload))
                        for response in body.get("responses", []):
                            correlation = response.get("correlationData")
                            if correlation in expected:
                                responses[correlation] = response
                        if len(responses) == len(expected):
                            break
            except TimeoutError as exc:
                raise BrokerAdminError(
                    "no response from the broker's dynamic-security API"
                ) from exc

        failures = [f"{expected[k]}: {r['error']}" for k, r in responses.items() if r.get("error")]
        if failures:
            raise BrokerAdminError("; ".join(failures))
        log.info("broker_admin_executed", commands=[c["command"] for c in commands])
        return [responses[k] for k in expected]
