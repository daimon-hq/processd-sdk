from __future__ import annotations

from typing import Any

import httpx

from .client import DaimonClient
from .exceptions import DaimonConnectionError, DaimonHttpError
from .models import ManagerCapacityResult, NetworkPolicy, SandboxInfo, ServicePortInfo


def _network_policy_body(policy: NetworkPolicy | dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalizes a user-supplied policy into a request body fragment."""
    if policy is None:
        return None
    if isinstance(policy, NetworkPolicy):
        return policy.to_dict()
    return dict(policy)


class ManagerHTTPTransport:
    def __init__(self, base_url: str, *, access_token: str | None, timeout_s: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout_s = timeout_s

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {"X-Access-Token": self.access_token} if self.access_token else None
        async with httpx.AsyncClient(
            headers=headers,
            timeout=self.timeout_s,
            follow_redirects=True,
        ) as client:
            try:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    content=content,
                    json=json,
                )
            except Exception as exc:  # pragma: no cover - transport failures vary
                raise DaimonConnectionError(str(exc)) from exc
        if response.status_code >= 400:
            payload: dict[str, Any] = {}
            try:
                raw_payload = response.json()
                if isinstance(raw_payload, dict):
                    payload = dict(raw_payload)
            except Exception:
                payload = {}
            message = payload.get("error")
            if not isinstance(message, str) or not message:
                message = response.text or f"http {response.status_code}"
            raise DaimonHttpError(
                message,
                status_code=response.status_code,
                payload=payload,
            )
        return response

    async def json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self.request(method, path, params=params, json=body)
        payload = response.json()
        if not isinstance(payload, dict):
            raise DaimonConnectionError(f"manager response was not a JSON object: {payload!r}")
        return payload


class DaimonSandbox:
    def __init__(
        self,
        manager: "DaimonManagerClient",
        info: SandboxInfo,
        *,
        timeout_s: float,
    ) -> None:
        self._manager = manager
        self.info = info
        self.client = DaimonClient(
            info.mcp_url,
            access_token=info.token,
            timeout_s=timeout_s,
        )

    @property
    def id(self) -> str:
        return self.info.id

    @property
    def service_ports(self) -> list[ServicePortInfo]:
        return self.info.service_ports

    def get_service_port(self, port: int) -> ServicePortInfo:
        for service_port in self.info.service_ports:
            if service_port.port == port:
                return service_port
        raise ValueError(f"service port is not available: {port}")

    @property
    def runtime(self):
        return self.client.runtime

    @property
    def files(self):
        return self.client.files

    @property
    def exec(self):
        return self.client.exec

    @property
    def web(self):
        return self.client.web

    @property
    def raw(self):
        return self.client.raw

    async def connect(self) -> "DaimonSandbox":
        await self.client.connect()
        return self

    async def close(self) -> None:
        await self.client.close()

    async def set_ttl(self, ttl_seconds: int) -> SandboxInfo:
        self.info = await self._manager.update_sandbox(self.id, ttl_seconds=ttl_seconds)
        return self.info

    async def refresh(self) -> SandboxInfo:
        self.info = await self._manager.get_sandbox(self.id)
        return self.info

    async def start(self) -> SandboxInfo:
        await self.close()
        self.info = await self._manager.start_sandbox(self.id)
        self.client = DaimonClient(
            self.info.mcp_url,
            access_token=self.info.token,
            timeout_s=self._manager.timeout_s,
        )
        return self.info

    async def stop(self) -> SandboxInfo:
        await self.close()
        self.info = await self._manager.stop_sandbox(self.id)
        return self.info

    async def delete(self) -> None:
        await self.close()
        await self._manager.delete_sandbox(self.id)
        self.info = SandboxInfo(
            id=self.info.id,
            state="deleted",
            mcp_url=self.info.mcp_url,
            token=self.info.token,
            workspace=self.info.workspace,
            created_at=self.info.created_at,
            limits=self.info.limits,
            labels=self.info.labels,
            service_ports=self.info.service_ports,
            last_used_at=self.info.last_used_at,
            ttl_seconds=self.info.ttl_seconds,
            expires_at=self.info.expires_at,
            raw_payload=self.info.raw_payload,
            action=self.info.action,
            network_policy=self.info.network_policy,
        )

    async def __aenter__(self) -> "DaimonSandbox":
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()


class SandboxContext:
    def __init__(
        self,
        manager: "DaimonManagerClient",
        *,
        delete_on_exit: bool,
        network_policy: NetworkPolicy | dict[str, Any] | None = None,
    ) -> None:
        self._manager = manager
        self._delete_on_exit = delete_on_exit
        self._network_policy = network_policy
        self._sandbox: DaimonSandbox | None = None

    async def __aenter__(self) -> DaimonSandbox:
        self._sandbox = await self._manager.create_sandbox(network_policy=self._network_policy)
        return await self._sandbox.connect()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._sandbox is None:
            return
        if self._delete_on_exit:
            await self._sandbox.delete()
        else:
            await self._sandbox.close()


class DaimonManagerClient:
    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout_s = timeout_s
        self._transport = ManagerHTTPTransport(
            self.base_url,
            access_token=access_token,
            timeout_s=timeout_s,
        )

    async def connect(self) -> "DaimonManagerClient":
        await self.health()
        return self

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> "DaimonManagerClient":
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def health(self) -> bool:
        response = await self._transport.request("GET", "/health")
        return response.status_code in (200, 204)

    async def capacity(self) -> ManagerCapacityResult:
        return ManagerCapacityResult.from_dict(await self._transport.json("GET", "/capacity"))

    def _sandbox_info(self, payload: dict[str, Any]) -> SandboxInfo:
        return SandboxInfo.from_dict(payload, base_url=self.base_url)

    async def create_sandbox(
        self,
        *,
        network_policy: NetworkPolicy | dict[str, Any] | None = None,
    ) -> DaimonSandbox:
        body: dict[str, Any] = {}
        policy_body = _network_policy_body(network_policy)
        if policy_body is not None:
            body["network_policy"] = policy_body
        info = self._sandbox_info(
            await self._transport.json("POST", "/sandboxes", body=body or None)
        )
        return DaimonSandbox(self, info, timeout_s=self.timeout_s)

    async def list_sandboxes(
        self,
        *,
        labels: dict[str, str] | None = None,
        states: list[str] | tuple[str, ...] | None = None,
    ) -> list[SandboxInfo]:
        params: dict[str, Any] = {}
        if states:
            params["state"] = list(states)
        for key, value in (labels or {}).items():
            params[f"label.{key}"] = value
        payload = await self._transport.json(
            "GET",
            "/sandboxes",
            params=params or None,
        )
        sandboxes = payload.get("sandboxes")
        if not isinstance(sandboxes, list):
            raise DaimonConnectionError("manager response missing sandboxes list")
        return [self._sandbox_info(dict(item)) for item in sandboxes]

    async def find_or_create_sandbox(
        self,
        *,
        labels: dict[str, str],
        ttl_seconds: int | None = None,
        network_policy: NetworkPolicy | dict[str, Any] | None = None,
    ) -> DaimonSandbox:
        body: dict[str, Any] = {"labels": labels}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        policy_body = _network_policy_body(network_policy)
        if policy_body is not None:
            # The manager refuses to reuse a sandbox whose effective policy
            # differs; a mismatch surfaces as DaimonHttpError(400).
            body["network_policy"] = policy_body
        info = self._sandbox_info(
            await self._transport.json("POST", "/sandboxes/find-or-create", body=body)
        )
        return DaimonSandbox(self, info, timeout_s=self.timeout_s)

    async def find_sandbox(
        self,
        *,
        labels: dict[str, str],
        ttl_seconds: int | None = None,
    ) -> DaimonSandbox:
        body: dict[str, Any] = {"labels": labels}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        info = self._sandbox_info(
            await self._transport.json("POST", "/sandboxes/find", body=body)
        )
        return DaimonSandbox(self, info, timeout_s=self.timeout_s)

    async def get_sandbox(self, sandbox_id: str) -> SandboxInfo:
        return self._sandbox_info(await self._transport.json("GET", f"/sandboxes/{sandbox_id}"))

    async def start_sandbox(self, sandbox_id: str) -> SandboxInfo:
        return self._sandbox_info(
            await self._transport.json("POST", f"/sandboxes/{sandbox_id}/start")
        )

    async def stop_sandbox(self, sandbox_id: str) -> SandboxInfo:
        return self._sandbox_info(
            await self._transport.json("POST", f"/sandboxes/{sandbox_id}/stop")
        )

    async def delete_sandbox(self, sandbox_id: str) -> None:
        await self._transport.request("DELETE", f"/sandboxes/{sandbox_id}")

    async def update_sandbox(
        self,
        sandbox_id: str,
        *,
        ttl_seconds: int | None = None,
        **updates: Any,
    ) -> SandboxInfo:
        body = {key: value for key, value in updates.items() if value is not None}
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        return self._sandbox_info(
            await self._transport.json("PATCH", f"/sandboxes/{sandbox_id}", body=body or None)
        )

    def sandbox(
        self,
        *,
        delete_on_exit: bool = True,
        network_policy: NetworkPolicy | dict[str, Any] | None = None,
    ) -> SandboxContext:
        return SandboxContext(
            self,
            delete_on_exit=delete_on_exit,
            network_policy=network_policy,
        )
