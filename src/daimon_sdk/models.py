from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from .exceptions import DaimonProtocolError

if TYPE_CHECKING:
    from .client import DaimonClient


_MISSING: Any = object()


def _str_list_field(payload: dict[str, Any], key: str, context: str) -> list[str]:
    """Reads a required-to-be-list-of-strings field, rejecting coercion-prone
    shapes (a bare string would silently explode into per-character entries).

    A missing key reads as the empty list; an explicit ``null`` is malformed
    (the manager's serde defaults only apply to omitted fields) and raises."""
    raw = payload.get(key, _MISSING)
    if raw is _MISSING:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise DaimonProtocolError(
            f"{context}.{key} must be a list of strings, got {type(raw).__name__}"
        )
    return [str(item) for item in raw]


def _bool_field(payload: dict[str, Any], key: str, default: bool, context: str) -> bool:
    """Reads a boolean field, rejecting truthy/falsy non-booleans like the
    string "false" (which ``bool()`` would read as True). A missing key reads
    as ``default``; an explicit ``null`` is malformed and raises."""
    raw = payload.get(key, _MISSING)
    if raw is _MISSING:
        return default
    if not isinstance(raw, bool):
        raise DaimonProtocolError(
            f"{context}.{key} must be a boolean, got {type(raw).__name__}"
        )
    return raw


@dataclass(slots=True)
class ContentBlock:
    type: str | None
    text: str | None = None
    mime_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ContentBlock":
        return cls(
            type=payload.get("type"),
            text=payload.get("text"),
            mime_type=payload.get("mimeType") or payload.get("mime_type"),
            raw=payload,
        )


@dataclass(slots=True)
class ReadTextFile:
    file_path: str
    content: str
    num_lines: int
    start_line: int
    total_lines: int


@dataclass(slots=True)
class ReadImageFile:
    file_path: str
    mime_type: str


@dataclass(slots=True)
class ReadPartsFile:
    file_path: str
    count: int
    pages: str


@dataclass(slots=True)
class ReadResult:
    kind: str
    file: ReadTextFile | ReadImageFile | ReadPartsFile
    extra_content: list[ContentBlock]
    raw_payload: dict[str, Any]
    content_blocks: list[ContentBlock] = field(default_factory=list)
    display_text: str = ""


@dataclass(slots=True)
class EditResult:
    file_path: str
    old_string: str
    new_string: str
    original_file: str
    structured_patch: list[dict[str, Any]]
    user_modified: bool
    replace_all: bool
    git_diff: dict[str, Any] | None
    raw_payload: dict[str, Any]
    content_blocks: list[ContentBlock] = field(default_factory=list)
    display_text: str = ""


@dataclass(slots=True)
class WriteResult:
    type: str
    file_path: str
    content: str
    structured_patch: list[dict[str, Any]]
    original_file: str | None
    git_diff: dict[str, Any] | None
    raw_payload: dict[str, Any]
    content_blocks: list[ContentBlock] = field(default_factory=list)
    display_text: str = ""


@dataclass(slots=True)
class GlobResult:
    search_path: str
    filenames: list[str]
    num_files: int
    truncated: bool
    duration_ms: int
    raw_payload: dict[str, Any]
    content_blocks: list[ContentBlock] = field(default_factory=list)
    display_text: str = ""


@dataclass(slots=True)
class GrepResult:
    mode: str
    filenames: list[str]
    num_files: int
    content: str | None
    num_lines: int | None
    num_matches: int | None
    applied_limit: int | None
    applied_offset: int | None
    raw_payload: dict[str, Any]
    content_blocks: list[ContentBlock] = field(default_factory=list)
    display_text: str = ""


@dataclass(slots=True)
class FileTransferResult:
    file_path: str
    bytes_written: int
    created_parent_directories: bool
    created: bool
    updated: bool
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class LimitsStatus:
    rlimit: str
    cgroup: str
    cgroup_reason: str | None
    raw_payload: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "LimitsStatus":
        payload = dict(payload or {})
        return cls(
            rlimit=str(payload.get("rlimit", "off")),
            cgroup=str(payload.get("cgroup", "off")),
            cgroup_reason=payload.get("cgroup_reason"),
            raw_payload=payload,
        )


@dataclass(slots=True)
class ServicePortInfo:
    port: int
    url: str
    token: str
    headers: dict[str, str]

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        token: str,
        base_url: str | None = None,
    ) -> "ServicePortInfo":
        return cls(
            port=int(payload["port"]),
            url=_normalize_manager_proxy_url(str(payload["url"]), base_url),
            token=token,
            headers={"X-Access-Token": token},
        )


@dataclass(slots=True)
class SecretRule:
    """Vault-style secret substitution rule, mirroring the manager's
    ``network_policy.secrets`` entries.

    The manager generates an opaque ``placeholder`` (``pdm-vlt-<hex>``) at
    creation; the jail environment variable named by the secrets-map key
    carries only that placeholder. The egress proxy substitutes the real
    ``value`` into outbound requests whose target matches ``allowed_hosts``
    (exact domains, ``*.`` suffix wildcards, literal IPs, or ``*``; CIDR
    ranges are not accepted for secret substitution), in request header
    values (``header``) and/or the request body (``body``). The real value
    never enters the sandbox; API responses redact it to ``"***"``.

    Literal IP ``allowed_hosts`` are intended for **plain HTTP** targets.
    HTTPS substitution needs a ClientHello SNI; clients connecting to an IP
    usually omit SNI, and processd refuses ClientHello without SNI. Prefer
    domain + HTTPS when the secret must be substituted over TLS.

    A rule parsed from an API response (``redacted``) cannot be re-sent:
    ``to_dict`` raises rather than silently re-creating a sandbox whose
    secret is the literal redaction marker.
    """

    value: str
    allowed_hosts: list[str]
    header: bool = True
    body: bool = True
    placeholder: str | None = None
    redacted: bool = False
    raw_payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SecretRule":
        payload = dict(payload)
        placeholder = payload.get("placeholder")
        if placeholder is not None and not isinstance(placeholder, str):
            raise DaimonProtocolError(
                "network_policy.secrets placeholder must be a string, "
                f"got {type(placeholder).__name__}"
            )
        value = payload.get("value", _MISSING)
        if value is _MISSING:
            value = ""
        if not isinstance(value, str):
            raise DaimonProtocolError(
                f"network_policy.secrets value must be a string, got {type(value).__name__}"
            )
        return cls(
            value=value,
            allowed_hosts=_str_list_field(payload, "allowed_hosts", "network_policy.secrets"),
            header=_bool_field(payload, "header", True, "network_policy.secrets"),
            body=_bool_field(payload, "body", True, "network_policy.secrets"),
            placeholder=None if placeholder in (None, "") else placeholder,
            redacted=value == "***",
            raw_payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        if self.redacted:
            raise ValueError(
                "this SecretRule was parsed from an API response with a redacted "
                'value ("***"); supply the real secret value before re-using it '
                "in a create request"
            )
        # The placeholder is generated server-side; callers never send one.
        return {
            "value": self.value,
            "allowed_hosts": list(self.allowed_hosts),
            "header": self.header,
            "body": self.body,
        }


@dataclass(slots=True)
class NetworkPolicy:
    """Per-sandbox egress policy, mirroring the manager's ``network_policy``.

    ``proxy`` (the manager default) blocks all direct egress; sandbox
    processes can only reach allow-listed targets through the manager's
    per-sandbox CONNECT proxy (an empty ``allow`` blocks all egress,
    ``["*"]`` allows everything). ``legacy_nat`` is the deprecated direct
    NAT+DNS mode with no egress control; its remaining fields are ignored by
    the manager.

    ``allow`` entries are exact hosts, ``*.`` suffix wildcards, literal IPs,
    CIDR ranges, or ``*``. The proxy dials whatever an allowed hostname
    resolves to — resolution results are NOT checked, so private/reserved
    address space is reachable by listing it explicitly OR by listing a
    hostname that resolves there; choose wildcard entries with care
    (pure-list semantics).
    """

    PROXY = "proxy"
    LEGACY_NAT = "legacy_nat"

    mode: str
    allow: list[str]
    allow_ports: list[int] | None
    secrets: dict[str, SecretRule]
    raw_payload: dict[str, Any]

    @classmethod
    def allow_all(cls) -> "NetworkPolicy":
        return cls(
            mode=cls.PROXY,
            allow=["*"],
            allow_ports=None,
            secrets={},
            raw_payload={},
        )

    @classmethod
    def proxy(
        cls,
        allow: list[str],
        *,
        allow_ports: list[int] | None = None,
        secrets: dict[str, SecretRule] | None = None,
    ) -> "NetworkPolicy":
        return cls(
            mode=cls.PROXY,
            allow=list(allow),
            allow_ports=list(allow_ports) if allow_ports is not None else None,
            secrets=dict(secrets) if secrets is not None else {},
            raw_payload={},
        )

    @classmethod
    def legacy_nat(cls) -> "NetworkPolicy":
        return cls(
            mode=cls.LEGACY_NAT,
            allow=[],
            allow_ports=None,
            secrets={},
            raw_payload={},
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "NetworkPolicy | None":
        if payload is None:
            return None
        payload = dict(payload)
        allow_ports = payload.get("allow_ports")
        if allow_ports is not None and (
            not isinstance(allow_ports, list)
            or not all(isinstance(port, int) and not isinstance(port, bool) for port in allow_ports)
        ):
            raise DaimonProtocolError(
                "network_policy.allow_ports must be a list of integers or null"
            )
        secrets_raw = payload.get("secrets", _MISSING)
        if secrets_raw is _MISSING:
            secrets_raw = {}
        if not isinstance(secrets_raw, dict) or not all(
            isinstance(rule, dict) for rule in secrets_raw.values()
        ):
            raise DaimonProtocolError(
                "network_policy.secrets must be an object of rule objects"
            )
        # A missing allow list mirrors the manager's serde default (["*"],
        # unrestricted) — NOT an empty list, which blocks all egress. An
        # explicit null is malformed (serde defaults only apply to omitted
        # fields) and raises inside _str_list_field.
        allow = (
            ["*"]
            if "allow" not in payload
            else _str_list_field(payload, "allow", "network_policy")
        )
        mode = payload.get("mode", cls.PROXY)
        if not isinstance(mode, str):
            raise DaimonProtocolError(
                f"network_policy.mode must be a string, got {type(mode).__name__}"
            )
        return cls(
            mode=mode,
            allow=allow,
            allow_ports=[int(port) for port in allow_ports]
            if allow_ports is not None
            else None,
            secrets={
                str(name): SecretRule.from_dict(rule) for name, rule in secrets_raw.items()
            },
            raw_payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        if self.mode == self.LEGACY_NAT:
            return {"mode": self.LEGACY_NAT}
        body: dict[str, Any] = {
            "mode": self.mode,
            "allow": list(self.allow),
        }
        if self.allow_ports is not None:
            body["allow_ports"] = list(self.allow_ports)
        if self.secrets:
            body["secrets"] = {name: rule.to_dict() for name, rule in self.secrets.items()}
        return body


class SandboxAction(StrEnum):
    """Which branch an actuating manager endpoint took to produce a SandboxInfo.

    Only set on `create`/`find-or-create`/`start`/`stop` responses; read-only
    endpoints (`get`/`list`/`find`/`patch`) leave it ``None``.
    """

    CREATED = "created"
    REUSED = "reused"
    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    STOPPED = "stopped"
    ALREADY_STOPPED = "already_stopped"


@dataclass(slots=True)
class SandboxInfo:
    id: str
    state: str
    mcp_url: str
    token: str
    workspace: str
    created_at: int
    limits: LimitsStatus
    labels: dict[str, str]
    service_ports: list[ServicePortInfo]
    last_used_at: int
    ttl_seconds: int | None
    expires_at: int | None
    raw_payload: dict[str, Any]
    action: SandboxAction | None = None
    network_policy: NetworkPolicy | None = None
    # Guest-facing egress CONNECT proxy (proxy mode only). Host controllers
    # that build in-sandbox configs (e.g. agentgateway backendTunnel) can use
    # these without executing a shell in the jail. Both are None when the
    # manager did not allocate a proxy (legacy_nat or older managers).
    proxy_port: int | None = None
    http_proxy: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, base_url: str | None = None) -> "SandboxInfo":
        token = str(payload["token"])
        proxy_port = _int_or_none(payload.get("proxy_port"))
        http_proxy = payload.get("http_proxy")
        if http_proxy is not None and not isinstance(http_proxy, str):
            raise DaimonProtocolError(
                f"sandbox http_proxy must be a string or null, got {type(http_proxy).__name__}"
            )
        if http_proxy is None and proxy_port is not None:
            # Older managers may only expose proxy_port; reconstruct the guest URL.
            http_proxy = f"http://127.0.0.1:{proxy_port}"
        return cls(
            id=str(payload["id"]),
            state=str(payload["state"]),
            mcp_url=_normalize_manager_proxy_url(str(payload["mcp_url"]), base_url),
            token=token,
            workspace=str(payload["workspace"]),
            created_at=int(payload["created_at"]),
            limits=LimitsStatus.from_dict(payload.get("limits")),
            labels={str(k): str(v) for k, v in dict(payload.get("labels") or {}).items()},
            service_ports=[
                ServicePortInfo.from_dict(dict(item), token=token, base_url=base_url)
                for item in list(payload.get("service_ports") or [])
            ],
            last_used_at=int(payload.get("last_used_at") or payload["created_at"]),
            ttl_seconds=_int_or_none(payload.get("ttl_seconds")),
            expires_at=_int_or_none(payload.get("expires_at")),
            raw_payload=dict(payload),
            action=_action_or_none(payload.get("action")),
            network_policy=NetworkPolicy.from_dict(payload.get("network_policy")),
            proxy_port=proxy_port,
            http_proxy=http_proxy if http_proxy else None,
        )


@dataclass(slots=True)
class CapacityResource:
    capacity: int | None
    reserve: int
    used: int
    available: int | None
    sandbox_request: int
    raw_payload: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CapacityResource":
        return cls(
            capacity=_int_or_none(payload.get("capacity")),
            reserve=int(payload.get("reserve", 0)),
            used=int(payload.get("used", 0)),
            available=_int_or_none(payload.get("available")),
            sandbox_request=int(payload.get("sandbox_request", 0)),
            raw_payload=dict(payload),
        )


@dataclass(slots=True)
class ManagerCapacityResult:
    mode: str
    capacity_source: str
    running_sandboxes: int
    creating_sandboxes: int
    memory_bytes: CapacityResource
    pids: CapacityResource
    cpu_ms_per_sec: CapacityResource
    raw_payload: dict[str, Any]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ManagerCapacityResult":
        return cls(
            mode=str(payload["mode"]),
            capacity_source=str(payload.get("capacity_source", "")),
            running_sandboxes=int(payload.get("running_sandboxes", 0)),
            creating_sandboxes=int(payload.get("creating_sandboxes", 0)),
            memory_bytes=CapacityResource.from_dict(payload.get("memory_bytes") or {}),
            pids=CapacityResource.from_dict(payload.get("pids") or {}),
            cpu_ms_per_sec=CapacityResource.from_dict(payload.get("cpu_ms_per_sec") or {}),
            raw_payload=dict(payload),
        )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _action_or_none(value: Any) -> SandboxAction | None:
    """Leniently parse an ``action`` value; unknown/missing values become None.

    Keeps older SDKs working if the manager later adds new action values. The
    original string is always preserved in ``SandboxInfo.raw_payload``.
    """
    if value is None:
        return None
    try:
        return SandboxAction(str(value))
    except ValueError:
        return None


def _normalize_manager_proxy_url(url: str, base_url: str | None) -> str:
    if not base_url:
        return url
    parsed = urlsplit(url)
    base = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or base.scheme not in {"http", "https"}:
        return url
    if parsed.hostname not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        return url
    return urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment))


@dataclass(slots=True)
class ExecResult:
    output: str
    wall_time_seconds: float
    chunk_id: str
    original_token_count: int
    session_id: int | None
    exit_code: int | None
    raw_payload: dict[str, Any]
    content_blocks: list[ContentBlock] = field(default_factory=list)
    display_text: str = ""

    @property
    def is_running(self) -> bool:
        return self.session_id is not None and self.exit_code is None

    @property
    def has_exited(self) -> bool:
        return self.exit_code is not None


@dataclass(slots=True)
class BashResult:
    stdout: str
    stderr: str
    interrupted: bool
    dangerously_disable_sandbox: bool
    persisted_output_path: str | None
    persisted_output_size: int | None
    background_task_id: str | None
    raw_payload: dict[str, Any]
    content_blocks: list[ContentBlock] = field(default_factory=list)
    display_text: str = ""

    @property
    def is_background(self) -> bool:
        return self.background_task_id is not None


@dataclass(slots=True)
class WebFetchResult:
    url: str
    status_code: int
    content_type: str
    bytes: int
    result_type: str
    content: str
    redirect_url: str | None
    persisted_path: str | None
    persisted_size: int | None
    duration_ms: int
    raw_payload: dict[str, Any]
    content_blocks: list[ContentBlock] = field(default_factory=list)
    display_text: str = ""


@dataclass(slots=True)
class RuntimeContextResult:
    payload: dict[str, Any]
    content_blocks: list[ContentBlock] = field(default_factory=list)
    display_text: str = ""

    @property
    def base_workdir(self) -> str | None:
        return self.payload.get("baseWorkdir")

    @property
    def summary(self) -> str | None:
        value = self.payload.get("summary")
        return value if isinstance(value, str) else None

    @property
    def filesystem(self) -> dict[str, Any]:
        value = self.payload.get("filesystem")
        return value if isinstance(value, dict) else {}

    @property
    def network(self) -> dict[str, Any]:
        value = self.payload.get("network")
        return value if isinstance(value, dict) else {}

    @property
    def capabilities(self) -> dict[str, Any]:
        value = self.payload.get("capabilities")
        return value if isinstance(value, dict) else {}


@dataclass(slots=True)
class SessionHandle:
    _client: "DaimonClient"
    session_id: int

    async def write(
        self,
        chars: str = "",
        *,
        yield_time_ms: int | None = None,
        max_output_tokens: int | None = None,
    ) -> ExecResult:
        return await self._client.exec.write_stdin(
            self.session_id,
            chars=chars,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
        )

    async def poll(
        self,
        *,
        yield_time_ms: int | None = None,
        max_output_tokens: int | None = None,
    ) -> ExecResult:
        return await self.write(
            "",
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
        )

    async def wait_for_exit(
        self,
        *,
        timeout_s: float = 10.0,
        yield_time_ms: int = 5_000,
        poll_interval_s: float = 0.05,
        max_output_tokens: int | None = None,
    ) -> ExecResult:
        deadline = time.monotonic() + timeout_s
        last_result: ExecResult | None = None
        while time.monotonic() < deadline:
            last_result = await self.poll(
                yield_time_ms=yield_time_ms,
                max_output_tokens=max_output_tokens,
            )
            if last_result.has_exited:
                return last_result
            await asyncio.sleep(poll_interval_s)
        raise TimeoutError(f"session {self.session_id} did not exit within {timeout_s} seconds")

    async def close(
        self,
        *,
        exit_payload: str = "__EXIT__\n",
        yield_time_ms: int = 500,
        max_output_tokens: int | None = None,
    ) -> ExecResult:
        return await self.write(
            exit_payload,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
        )
