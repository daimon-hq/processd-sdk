from __future__ import annotations

import threading
import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from daimon_sdk import DaimonClient
from daimon_sdk._transport import (
    _USE_DEFAULT_TIMEOUT,
    FastMCPTransportAdapter,
    ToolCallEnvelope,
    _consume_connect_exception,
    content_blocks_display_text,
    decode_tool_result,
)
from daimon_sdk.exceptions import DaimonConnectionError, DaimonHttpError, DaimonProtocolError
from daimon_sdk.manager import DaimonManagerClient, DaimonSandbox, ManagerHTTPTransport
from daimon_sdk.models import (
    ExecResult,
    ManagerCapacityResult,
    NetworkPolicy,
    SandboxAction,
    SandboxInfo,
    SecretRule,
    SessionHandle,
)


class DummyText:
    def __init__(self, text: str) -> None:
        self.text = text
        self.type = "text"


class DummyResult:
    def __init__(self, *, structured_content=None, data=None, content=None) -> None:
        self.structured_content = structured_content
        self.data = data
        self.content = content or []


SANDBOX_PAYLOAD = {
    "id": "sandbox-1",
    "state": "running",
    "mcp_url": "http://127.0.0.1:19000/mcp",
    "token": "pdm-token",
    "workspace": "/tmp/processd-manager/workspaces/sandbox-1/workspace",
    "created_at": 123,
    "labels": {"thread_id": "thread-a"},
    "last_used_at": 124,
    "ttl_seconds": 3600,
    "expires_at": 3724,
    "action": "reused",
    "limits": {
        "rlimit": "applied",
        "cgroup": "applied",
        "cgroup_reason": None,
    },
    "sandbox_ip": "10.255.0.2",
    "network_policy": {
        "mode": "proxy",
        "allow": ["example.com"],
        "allow_ports": [80, 443],
        "secrets": {
            "API_KEY": {
                "placeholder": "pdm-vlt-0123456789abcdef0123456789abcdef",
                "value": "***",
                "allowed_hosts": ["api.example.com"],
                "header": True,
                "body": False,
            }
        },
    },
    "service_ports": [
        {
            "port": 3000,
            "host_port": 19001,
            "url": "http://127.0.0.1:18080/sandboxes/sandbox-1/ports/3000/",
            "created_at": 125,
            "updated_at": 126,
        },
        {
            "port": 3001,
            "host_port": 19002,
            "url": "http://127.0.0.1:18080/sandboxes/sandbox-1/ports/3001/",
            "created_at": 125,
            "updated_at": 126,
        },
    ],
}


CAPACITY_PAYLOAD = {
    "mode": "resource",
    "capacity_source": "/sys/fs/cgroup/test",
    "running_sandboxes": 1,
    "creating_sandboxes": 0,
    "memory_bytes": {
        "capacity": 8589934592,
        "reserve": 536870912,
        "used": 2147483648,
        "available": 5905580032,
        "sandbox_request": 2147483648,
    },
    "pids": {
        "capacity": 4096,
        "reserve": 128,
        "used": 256,
        "available": 3712,
        "sandbox_request": 256,
    },
    "cpu_ms_per_sec": {
        "capacity": 4000,
        "reserve": 500,
        "used": 1000,
        "available": 2500,
        "sandbox_request": 1000,
    },
}


class _QuietHTTPServer(HTTPServer):
    allow_reuse_address = True


class _ErrorHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        body = {
            "error": "insufficient sandbox manager capacity",
            "admission": {
                "missing": [
                    {
                        "resource": "memory_bytes",
                        "required": 999,
                        "available": 1,
                        "capacity": 100,
                        "reserve": 10,
                        "used": 89,
                    }
                ]
            },
        }
        encoded = json.dumps(body).encode()
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class _CreateSandboxHandler(BaseHTTPRequestHandler):
    """Records request bodies and replies with the canonical sandbox payload."""

    def log_message(self, format: str, *args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.captured_requests.append((self.path, body))
        encoded = json.dumps(SANDBOX_PAYLOAD).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _start_capture_server() -> tuple[_QuietHTTPServer, threading.Thread]:
    server = _QuietHTTPServer(("127.0.0.1", 0), _CreateSandboxHandler)
    server.captured_requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class DummyClient:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


def test_decode_tool_result_prefers_structured_content() -> None:
    payload, content = decode_tool_result(
        DummyResult(
            structured_content={"ok": True},
            content=[DummyText(json.dumps({"ok": True}))],
        )
    )
    assert payload == {"ok": True}
    assert content[0]["type"] == "text"


def test_content_blocks_display_text_joins_text_blocks() -> None:
    assert (
        content_blocks_display_text(
            [
                {"type": "text", "text": "first"},
                {"type": "image", "data": "..."},
                {"type": "text", "text": "second"},
            ]
        )
        == "first\nsecond"
    )


def test_decode_tool_result_raises_for_invalid_text_json() -> None:
    with pytest.raises(DaimonProtocolError):
        decode_tool_result(DummyResult(content=[DummyText("not-json")]))


@pytest.mark.asyncio
async def test_typed_tool_results_include_display_text() -> None:
    client = DaimonClient("http://127.0.0.1:19000/mcp")

    async def fake_call_tool(name: str, arguments: dict, *, raise_on_error: bool = True):
        assert name == "Write"
        return ToolCallEnvelope(
            tool_name=name,
            payload={
                "type": "create",
                "filePath": "/workspace/a.txt",
                "content": "hello\n",
                "structuredPatch": [],
            },
            content_blocks=[{"type": "text", "text": "Created file /workspace/a.txt"}],
            display_text="Created file /workspace/a.txt",
            raw_result=None,
        )

    client._call_tool = fake_call_tool  # type: ignore[method-assign]
    written = await client.files.write("/workspace/a.txt", "hello\n")
    assert written.display_text == "Created file /workspace/a.txt"
    assert written.content_blocks[0].text == "Created file /workspace/a.txt"


def test_manager_models_parse_payloads() -> None:
    sandbox = SandboxInfo.from_dict(SANDBOX_PAYLOAD)
    assert sandbox.id == "sandbox-1"
    assert sandbox.limits.rlimit == "applied"
    assert sandbox.labels["thread_id"] == "thread-a"
    assert sandbox.ttl_seconds == 3600
    assert sandbox.expires_at == 3724
    assert sandbox.action == SandboxAction.REUSED
    assert sandbox.action == "reused"
    assert sandbox.raw_payload["mcp_url"] == SANDBOX_PAYLOAD["mcp_url"]
    assert len(sandbox.service_ports) == 2
    assert sandbox.service_ports[0].port == 3000
    assert sandbox.service_ports[0].url == (
        "http://127.0.0.1:18080/sandboxes/sandbox-1/ports/3000/"
    )
    assert sandbox.service_ports[0].token == "pdm-token"
    assert sandbox.service_ports[0].headers == {"X-Access-Token": "pdm-token"}
    assert not hasattr(sandbox.service_ports[0], "host_port")
    assert not hasattr(sandbox.service_ports[0], "sandbox_ip")
    assert not hasattr(sandbox.service_ports[0], "mcp_url")

    policy = sandbox.network_policy
    assert policy is not None
    assert policy.mode == "proxy"
    assert policy.allow == ["example.com"]
    assert policy.allow_ports == [80, 443]
    assert policy.raw_payload["mode"] == "proxy"
    # Secrets parse with the redacted value and the echoed placeholder.
    rule = policy.secrets["API_KEY"]
    assert rule.value == "***"
    assert rule.placeholder == "pdm-vlt-0123456789abcdef0123456789abcdef"
    assert rule.allowed_hosts == ["api.example.com"]
    assert rule.header is True
    assert rule.body is False

    capacity = ManagerCapacityResult.from_dict(CAPACITY_PAYLOAD)
    assert capacity.mode == "resource"
    assert capacity.memory_bytes.capacity == 8589934592
    assert capacity.cpu_ms_per_sec.available == 2500
    assert capacity.raw_payload["capacity_source"] == "/sys/fs/cgroup/test"


def test_sandbox_info_network_policy_defaults_to_none_when_missing() -> None:
    payload = {key: value for key, value in SANDBOX_PAYLOAD.items() if key != "network_policy"}
    sandbox = SandboxInfo.from_dict(payload)
    assert sandbox.network_policy is None


def test_network_policy_to_dict_shapes() -> None:
    # Allow-all proxy mode (the manager's default networking).
    assert NetworkPolicy.allow_all().to_dict() == {
        "mode": "proxy",
        "allow": ["*"],
    }
    # legacy_nat drops all other fields.
    assert NetworkPolicy.legacy_nat().to_dict() == {"mode": "legacy_nat"}
    # None fields are omitted so the manager applies its defaults.
    assert NetworkPolicy.proxy(["example.com"]).to_dict() == {
        "mode": "proxy",
        "allow": ["example.com"],
    }
    assert NetworkPolicy.proxy(
        ["example.com", "*.rust-lang.org"],
        allow_ports=[443],
        secrets={
            "API_KEY": SecretRule(
                value="sk-real",
                allowed_hosts=["api.example.com"],
                body=False,
            )
        },
    ).to_dict() == {
        "mode": "proxy",
        "allow": ["example.com", "*.rust-lang.org"],
        "allow_ports": [443],
        "secrets": {
            "API_KEY": {
                "value": "sk-real",
                "allowed_hosts": ["api.example.com"],
                "header": True,
                "body": False,
            }
        },
    }
    # An empty allow list is meaningful (blocks all egress) and must be sent
    # explicitly.
    assert NetworkPolicy.proxy([]).to_dict() == {
        "mode": "proxy",
        "allow": [],
    }
    # The placeholder is generated server-side and never sent by the client.
    assert "placeholder" not in SecretRule(
        value="sk-real",
        allowed_hosts=["api.example.com"],
        placeholder="pdm-vlt-echo",
    ).to_dict()


def test_secret_rule_redacted_response_cannot_be_reserialized() -> None:
    # A rule parsed from an API response carries the redacted "***" value;
    # re-sending it would silently create a sandbox whose secret is the
    # literal redaction marker.
    rule = SecretRule.from_dict(
        {
            "placeholder": "pdm-vlt-abc",
            "value": "***",
            "allowed_hosts": ["api.example.com"],
            "header": True,
            "body": True,
        }
    )
    assert rule.redacted is True
    with pytest.raises(ValueError, match="redacted"):
        rule.to_dict()
    policy = NetworkPolicy.from_dict(
        {
            "mode": "proxy",
            "allow": ["api.example.com"],
            "secrets": {"API_KEY": rule.raw_payload},
        }
    )
    assert policy is not None
    with pytest.raises(ValueError, match="redacted"):
        policy.to_dict()


def test_network_policy_from_dict_rejects_malformed_types() -> None:
    # A bare string must not explode into per-character entries.
    with pytest.raises(DaimonProtocolError):
        NetworkPolicy.from_dict({"mode": "proxy", "allow": "example.com"})
    with pytest.raises(DaimonProtocolError):
        NetworkPolicy.from_dict({"mode": "proxy", "allow_ports": "443"})
    with pytest.raises(DaimonProtocolError):
        NetworkPolicy.from_dict({"mode": "proxy", "secrets": {"K": "not-a-dict"}})
    # Falsy-but-malformed shapes must not slip past validation either.
    with pytest.raises(DaimonProtocolError):
        NetworkPolicy.from_dict({"mode": "proxy", "secrets": []})
    with pytest.raises(DaimonProtocolError):
        SecretRule.from_dict({"value": 0, "allowed_hosts": ["a.example.com"]})
    with pytest.raises(DaimonProtocolError):
        SecretRule.from_dict({"value": False, "allowed_hosts": ["a.example.com"]})
    # Truthy strings are not booleans.
    with pytest.raises(DaimonProtocolError):
        SecretRule.from_dict(
            {"value": "x", "allowed_hosts": ["a.example.com"], "header": "false"}
        )
    with pytest.raises(DaimonProtocolError):
        SecretRule.from_dict(
            {"value": "x", "allowed_hosts": "a.example.com"}
        )


def test_network_policy_from_dict_missing_allow_defaults_to_allow_all() -> None:
    # The manager's serde default for an omitted allow list is ["*"]
    # (unrestricted) — parsing and re-sending must not silently flip it to
    # block-all.
    policy = NetworkPolicy.from_dict({"mode": "proxy"})
    assert policy is not None
    assert policy.allow == ["*"]
    assert policy.to_dict() == {"mode": "proxy", "allow": ["*"]}


def test_network_policy_from_dict_rejects_explicit_nulls() -> None:
    # Serde defaults apply only to OMITTED fields; an explicit JSON null is
    # malformed and must not silently become the default.
    with pytest.raises(DaimonProtocolError):
        NetworkPolicy.from_dict({"mode": "proxy", "allow": None})
    with pytest.raises(DaimonProtocolError):
        NetworkPolicy.from_dict({"mode": "proxy", "secrets": None})
    with pytest.raises(DaimonProtocolError):
        SecretRule.from_dict({"value": None, "allowed_hosts": ["a.example.com"]})
    with pytest.raises(DaimonProtocolError):
        SecretRule.from_dict({"value": "x", "allowed_hosts": None})
    with pytest.raises(DaimonProtocolError):
        SecretRule.from_dict(
            {"value": "x", "allowed_hosts": ["a.example.com"], "header": None}
        )
    with pytest.raises(DaimonProtocolError):
        SecretRule.from_dict(
            {"value": "x", "allowed_hosts": ["a.example.com"], "body": None}
        )


def test_network_policy_from_dict_rejects_non_string_scalars() -> None:
    # mode and placeholder are string fields; str() must not coerce other types.
    with pytest.raises(DaimonProtocolError):
        NetworkPolicy.from_dict({"mode": False})
    with pytest.raises(DaimonProtocolError):
        NetworkPolicy.from_dict({"mode": 0})
    with pytest.raises(DaimonProtocolError):
        SecretRule.from_dict(
            {"value": "x", "allowed_hosts": ["a.example.com"], "placeholder": False}
        )


def test_sandbox_info_action_defaults_to_none_when_missing() -> None:
    payload = {key: value for key, value in SANDBOX_PAYLOAD.items() if key != "action"}
    sandbox = SandboxInfo.from_dict(payload)
    assert sandbox.action is None


def test_sandbox_info_action_is_none_for_unknown_value() -> None:
    payload = {**SANDBOX_PAYLOAD, "action": "some_future_action"}
    sandbox = SandboxInfo.from_dict(payload)
    assert sandbox.action is None
    assert sandbox.raw_payload["action"] == "some_future_action"


@pytest.mark.parametrize("action", list(SandboxAction))
def test_sandbox_info_action_round_trips_all_wire_values(action: SandboxAction) -> None:
    payload = {**SANDBOX_PAYLOAD, "action": action.value}
    sandbox = SandboxInfo.from_dict(payload)
    assert sandbox.action is action
    assert sandbox.action == action.value


@pytest.mark.parametrize("bad_value", [0, 1, True, ["reused"], {"a": 1}, b"reused"])
def test_sandbox_info_action_is_none_for_non_string_values(bad_value) -> None:
    payload = {**SANDBOX_PAYLOAD, "action": bad_value}
    sandbox = SandboxInfo.from_dict(payload)
    assert sandbox.action is None


def test_sandbox_info_rewrites_localhost_proxy_urls_from_manager_base_url() -> None:
    sandbox = SandboxInfo.from_dict(
        SANDBOX_PAYLOAD,
        base_url="http://192.168.4.250:18080",
    )

    assert sandbox.mcp_url == "http://192.168.4.250:18080/mcp"
    assert sandbox.service_ports[0].url == (
        "http://192.168.4.250:18080/sandboxes/sandbox-1/ports/3000/"
    )
    assert sandbox.service_ports[0].headers == {"X-Access-Token": "pdm-token"}


@pytest.mark.asyncio
async def test_manager_http_transport_preserves_429_payload() -> None:
    server = _QuietHTTPServer(("127.0.0.1", 0), _ErrorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = ManagerHTTPTransport(
            f"http://127.0.0.1:{server.server_address[1]}",
            access_token="secret",
            timeout_s=5,
        )
        with pytest.raises(DaimonHttpError) as exc_info:
            await transport.request("POST", "/sandboxes")
        assert exc_info.value.status_code == 429
        assert exc_info.value.payload["admission"]["missing"][0]["resource"] == "memory_bytes"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_create_sandbox_sends_no_body_without_policy() -> None:
    server, thread = _start_capture_server()
    try:
        manager = DaimonManagerClient(f"http://127.0.0.1:{server.server_address[1]}")
        sandbox = await manager.create_sandbox()
        assert server.captured_requests == [("/sandboxes", b"")]
        # The response policy is parsed onto the sandbox info.
        assert sandbox.info.network_policy is not None
        assert sandbox.info.network_policy.mode == "proxy"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_create_sandbox_sends_network_policy_body() -> None:
    server, thread = _start_capture_server()
    try:
        manager = DaimonManagerClient(f"http://127.0.0.1:{server.server_address[1]}")
        await manager.create_sandbox(
            network_policy=NetworkPolicy.proxy(["example.com"], allow_ports=[443])
        )
        path, body = server.captured_requests[0]
        assert path == "/sandboxes"
        assert json.loads(body) == {
            "network_policy": {
                "mode": "proxy",
                "allow": ["example.com"],
                "allow_ports": [443],
            }
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_create_sandbox_accepts_dict_policy() -> None:
    server, thread = _start_capture_server()
    try:
        manager = DaimonManagerClient(f"http://127.0.0.1:{server.server_address[1]}")
        await manager.create_sandbox(network_policy={"mode": "proxy", "allow": []})
        _, body = server.captured_requests[0]
        assert json.loads(body) == {"network_policy": {"mode": "proxy", "allow": []}}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_create_sandbox_strips_placeholders_from_dict_policy() -> None:
    server, thread = _start_capture_server()
    try:
        manager = DaimonManagerClient(f"http://127.0.0.1:{server.server_address[1]}")
        # A response-shaped policy dict (e.g. copied from raw_payload) carries
        # server-generated placeholders; they must never be re-sent.
        await manager.create_sandbox(
            network_policy={
                "mode": "proxy",
                "allow": ["api.example.com"],
                "secrets": {
                    "API_KEY": {
                        "placeholder": "pdm-vlt-abc",
                        "value": "sk-real",
                        "allowed_hosts": ["api.example.com"],
                        "header": True,
                        "body": True,
                    }
                },
            }
        )
        _, body = server.captured_requests[0]
        sent = json.loads(body)["network_policy"]["secrets"]["API_KEY"]
        assert "placeholder" not in sent
        assert sent["value"] == "sk-real"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_create_sandbox_rejects_redacted_value_in_dict_policy() -> None:
    server, thread = _start_capture_server()
    try:
        manager = DaimonManagerClient(f"http://127.0.0.1:{server.server_address[1]}")
        # A policy dict copied from an API response carries redacted "***"
        # values; replaying it would silently set the real secret to the
        # redaction marker, so it must be rejected client-side.
        with pytest.raises(ValueError, match="redacted"):
            await manager.create_sandbox(
                network_policy={
                    "mode": "proxy",
                    "allow": ["api.example.com"],
                    "secrets": {
                        "API_KEY": {
                            "placeholder": "pdm-vlt-abc",
                            "value": "***",
                            "allowed_hosts": ["api.example.com"],
                        }
                    },
                }
            )
        assert server.captured_requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_find_or_create_sandbox_includes_network_policy() -> None:
    server, thread = _start_capture_server()
    try:
        manager = DaimonManagerClient(f"http://127.0.0.1:{server.server_address[1]}")
        await manager.find_or_create_sandbox(
            labels={"thread_id": "thread-a"},
            network_policy=NetworkPolicy.proxy(["example.com"]),
        )
        path, body = server.captured_requests[0]
        assert path == "/sandboxes/find-or-create"
        assert json.loads(body) == {
            "labels": {"thread_id": "thread-a"},
            "network_policy": {"mode": "proxy", "allow": ["example.com"]},
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_daimon_sandbox_service_port_helpers() -> None:
    info = SandboxInfo.from_dict(SANDBOX_PAYLOAD)
    sandbox = DaimonSandbox.__new__(DaimonSandbox)
    sandbox.info = info

    service_port = sandbox.get_service_port(3000)
    assert service_port == sandbox.service_ports[0]

    with pytest.raises(ValueError, match="service port is not available: 3010"):
        sandbox.get_service_port(3010)


@pytest.mark.asyncio
async def test_session_handle_wait_for_exit_times_out() -> None:
    class DummyExecAPI:
        async def write_stdin(self, session_id: int, *, chars: str = "", yield_time_ms=None, max_output_tokens=None):
            return ExecResult(
                output="",
                wall_time_seconds=0.0,
                chunk_id="x",
                original_token_count=0,
                session_id=session_id,
                exit_code=None,
                raw_payload={},
            )

    class DummyClient:
        exec = DummyExecAPI()

    handle = SessionHandle(DummyClient(), 123)
    with pytest.raises(TimeoutError):
        await handle.wait_for_exit(timeout_s=0.01, yield_time_ms=1, poll_interval_s=0.001)


@pytest.mark.asyncio
async def test_daimon_sandbox_lifecycle_updates_info_and_closes_client() -> None:
    class DummyManager:
        timeout_s = 30.0

        async def start_sandbox(self, sandbox_id: str) -> SandboxInfo:
            payload = dict(SANDBOX_PAYLOAD)
            payload["id"] = sandbox_id
            payload["state"] = "running"
            payload["mcp_url"] = "http://127.0.0.1:19001/mcp"
            return SandboxInfo.from_dict(payload)

        async def stop_sandbox(self, sandbox_id: str) -> SandboxInfo:
            payload = dict(SANDBOX_PAYLOAD)
            payload["id"] = sandbox_id
            payload["state"] = "stopped"
            return SandboxInfo.from_dict(payload)

        async def delete_sandbox(self, sandbox_id: str) -> None:
            self.deleted = sandbox_id

        async def update_sandbox(self, sandbox_id: str, *, ttl_seconds: int | None = None, **updates) -> SandboxInfo:
            payload = dict(SANDBOX_PAYLOAD)
            payload["id"] = sandbox_id
            payload["ttl_seconds"] = ttl_seconds
            payload["expires_at"] = payload["last_used_at"] + ttl_seconds if ttl_seconds is not None else payload["expires_at"]
            return SandboxInfo.from_dict(payload)

    manager = DummyManager()
    sandbox = DaimonSandbox(manager, SandboxInfo.from_dict(SANDBOX_PAYLOAD), timeout_s=30)
    first_client = DummyClient()
    sandbox.client = first_client

    stopped = await sandbox.stop()
    assert stopped.state == "stopped"
    assert first_client.closed == 1

    restarted = await sandbox.start()
    assert restarted.state == "running"
    assert sandbox.info.mcp_url == "http://127.0.0.1:19001/mcp"

    replacement_client = DummyClient()
    sandbox.client = replacement_client
    await sandbox.delete()
    assert manager.deleted == "sandbox-1"
    assert replacement_client.closed == 1
    assert sandbox.info.state == "deleted"

    updated = await sandbox.set_ttl(0)
    assert updated.ttl_seconds == 0
    assert sandbox.info.ttl_seconds == 0


@pytest.mark.asyncio
async def test_manager_list_sandboxes_gets_filtered_sandbox_list(monkeypatch) -> None:
    from daimon_sdk.manager import DaimonManagerClient

    captured: dict[str, object] = {}
    second = dict(SANDBOX_PAYLOAD)
    second["id"] = "sandbox-2"
    second["state"] = "stopped"

    async def fake_json(method: str, path: str, *, params=None, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        captured["body"] = body
        return {"sandboxes": [dict(SANDBOX_PAYLOAD), second]}

    manager = DaimonManagerClient("http://127.0.0.1:18080")
    monkeypatch.setattr(manager._transport, "json", fake_json)

    sandboxes = await manager.list_sandboxes(
        labels={"thread_id": "thread-a"},
        states=["running", "stopped"],
    )

    assert [sandbox.id for sandbox in sandboxes] == ["sandbox-1", "sandbox-2"]
    assert sandboxes[1].state == "stopped"
    assert captured == {
        "method": "GET",
        "path": "/sandboxes",
        "params": {
            "state": ["running", "stopped"],
            "label.thread_id": "thread-a",
        },
        "body": None,
    }


@pytest.mark.asyncio
async def test_manager_find_sandbox_posts_labels_without_ttl_by_default(monkeypatch) -> None:
    from daimon_sdk.manager import DaimonManagerClient, DaimonSandbox

    captured: dict[str, object] = {}

    async def fake_json(method: str, path: str, *, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return dict(SANDBOX_PAYLOAD)

    manager = DaimonManagerClient("http://127.0.0.1:18080")
    monkeypatch.setattr(manager._transport, "json", fake_json)

    sandbox = await manager.find_sandbox(labels={"thread_id": "thread-a"})

    assert isinstance(sandbox, DaimonSandbox)
    assert sandbox.id == "sandbox-1"
    assert captured == {
        "method": "POST",
        "path": "/sandboxes/find",
        "body": {"labels": {"thread_id": "thread-a"}},
    }


@pytest.mark.asyncio
async def test_manager_find_sandbox_posts_explicit_ttl_for_compat_refresh(monkeypatch) -> None:
    from daimon_sdk.manager import DaimonManagerClient, DaimonSandbox

    captured: dict[str, object] = {}

    async def fake_json(method: str, path: str, *, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return dict(SANDBOX_PAYLOAD)

    manager = DaimonManagerClient("http://127.0.0.1:18080")
    monkeypatch.setattr(manager._transport, "json", fake_json)

    sandbox = await manager.find_sandbox(
        labels={"thread_id": "thread-a"},
        ttl_seconds=60,
    )

    assert isinstance(sandbox, DaimonSandbox)
    assert sandbox.id == "sandbox-1"
    assert captured == {
        "method": "POST",
        "path": "/sandboxes/find",
        "body": {"labels": {"thread_id": "thread-a"}, "ttl_seconds": 60},
    }


@pytest.mark.asyncio
async def test_manager_update_sandbox_omits_none_fields(monkeypatch) -> None:
    from daimon_sdk.manager import DaimonManagerClient

    captured: dict[str, object] = {}

    async def fake_json(method: str, path: str, *, body=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return dict(SANDBOX_PAYLOAD)

    manager = DaimonManagerClient("http://127.0.0.1:18080")
    monkeypatch.setattr(manager._transport, "json", fake_json)

    await manager.update_sandbox("sandbox-1", ttl_seconds=0, note=None)

    assert captured == {
        "method": "PATCH",
        "path": "/sandboxes/sandbox-1",
        "body": {"ttl_seconds": 0},
    }


@pytest.mark.asyncio
async def test_manager_sandbox_context_deletes_on_exception(monkeypatch) -> None:
    from daimon_sdk.manager import DaimonManagerClient, DaimonSandbox

    calls: list[str] = []

    async def fake_create(self, **kwargs):
        return DaimonSandbox(self, SandboxInfo.from_dict(SANDBOX_PAYLOAD), timeout_s=30)

    async def fake_connect(self):
        calls.append("connect")
        return self

    async def fake_delete(self):
        calls.append("delete")

    monkeypatch.setattr(DaimonManagerClient, "create_sandbox", fake_create)
    monkeypatch.setattr(DaimonSandbox, "connect", fake_connect)
    monkeypatch.setattr(DaimonSandbox, "delete", fake_delete)

    manager = DaimonManagerClient("http://127.0.0.1:18080")
    with pytest.raises(RuntimeError):
        async with manager.sandbox() as sandbox:
            assert sandbox.id == "sandbox-1"
            raise RuntimeError("boom")
    assert calls == ["connect", "delete"]


@pytest.mark.asyncio
async def test_manager_sandbox_context_can_leave_sandbox_running(monkeypatch) -> None:
    from daimon_sdk.manager import DaimonManagerClient, DaimonSandbox

    calls: list[str] = []

    async def fake_create(self, **kwargs):
        return DaimonSandbox(self, SandboxInfo.from_dict(SANDBOX_PAYLOAD), timeout_s=30)

    async def fake_connect(self):
        calls.append("connect")
        return self

    async def fake_close(self):
        calls.append("close")

    async def fake_delete(self):
        calls.append("delete")

    monkeypatch.setattr(DaimonManagerClient, "create_sandbox", fake_create)
    monkeypatch.setattr(DaimonSandbox, "connect", fake_connect)
    monkeypatch.setattr(DaimonSandbox, "close", fake_close)
    monkeypatch.setattr(DaimonSandbox, "delete", fake_delete)

    manager = DaimonManagerClient("http://127.0.0.1:18080")
    async with manager.sandbox(delete_on_exit=False):
        pass
    assert calls == ["connect", "close"]


@pytest.mark.asyncio
async def test_transport_call_tool_threads_per_call_timeout(monkeypatch) -> None:
    """call_tool must forward the per-call read timeout to the FastMCP client.

    Default falls back to the configured timeout_s; an explicit float overrides
    it; an explicit None requests an unbounded read.
    """
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=10.0)
    seen: list[object] = []

    class FakeFastMCPClient:
        async def call_tool(self, name, arguments, *, raise_on_error, timeout):
            seen.append(timeout)
            return DummyResult(structured_content={"ok": True})

    async def fake_connect(self) -> None:
        adapter._client = FakeFastMCPClient()  # type: ignore[assignment]

    monkeypatch.setattr(FastMCPTransportAdapter, "connect", fake_connect)

    await adapter.call_tool("Bash", {})
    assert seen[-1] == 10.0

    await adapter.call_tool("Bash", {}, timeout=305.0)
    assert seen[-1] == 305.0

    await adapter.call_tool("Bash", {}, timeout=None)
    assert seen[-1] is None


def test_httpx_factory_leaves_read_unbounded_for_tool_calls() -> None:
    """Tool-call HTTP reads are bounded by the MCP session timeout, not httpx."""
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=10.0)
    client = adapter._httpx_client_factory()
    assert client.timeout.read is None
    assert client.timeout.connect == 10.0
    assert client.timeout.write == 10.0
    assert client.timeout.pool == 10.0


def test_httpx_factory_respects_explicit_bounded_timeout() -> None:
    """Direct request/stream helpers pass an explicit bounded timeout."""
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=10.0)
    client = adapter._httpx_client_factory(timeout=httpx.Timeout(10.0))
    assert client.timeout.read == 10.0
    assert client.timeout.connect == 10.0


@pytest.mark.asyncio
async def test_bash_threads_transport_timeout_to_call_tool() -> None:
    client = DaimonClient("http://127.0.0.1:19000/mcp", timeout_s=10.0)
    seen: list[object] = []

    async def fake_call_tool(name, arguments, *, raise_on_error=True, timeout=_USE_DEFAULT_TIMEOUT):
        seen.append(timeout)
        return ToolCallEnvelope(
            tool_name=name,
            payload={
                "stdout": "ok",
                "stderr": "",
                "interrupted": False,
                "dangerouslyDisableSandbox": False,
            },
            content_blocks=[],
            display_text="",
            raw_result=None,
        )

    client._call_tool = fake_call_tool  # type: ignore[method-assign]

    await client.exec.bash("make build", timeout_ms=300000, transport_timeout_s=305.0)
    assert seen[-1] == 305.0

    await client.exec.bash("long-job", timeout_ms=None, transport_timeout_s=None)
    assert seen[-1] is None

    await client.exec.bash("pwd")
    assert seen[-1] is _USE_DEFAULT_TIMEOUT


@pytest.mark.asyncio
async def test_concurrent_connect_builds_single_client(monkeypatch) -> None:
    """Concurrent first connects must not each build (and leak) a client."""
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=10.0)
    built: list[object] = []

    class FakeClient:
        async def __aenter__(self):
            # Yield so a lockless connect would interleave a second build here.
            await asyncio.sleep(0.05)
            return self

    def fake_client_ctor(transport, init_timeout=None):
        client = FakeClient()
        built.append(client)
        return client

    monkeypatch.setattr("daimon_sdk._transport.Client", fake_client_ctor)

    await asyncio.gather(adapter.connect(), adapter.connect(), adapter.connect())
    assert len(built) == 1
    assert adapter._client is built[0]


@pytest.mark.asyncio
async def test_connect_timeout_error_is_descriptive(monkeypatch) -> None:
    """A stalled connect must surface a real message, not an empty TimeoutError."""
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=0.05)

    class FakeClient:
        async def __aenter__(self):
            await asyncio.sleep(10)  # stall past the connect cap
            return self

    monkeypatch.setattr("daimon_sdk._transport.Client", lambda *a, **k: FakeClient())

    with pytest.raises(DaimonConnectionError) as excinfo:
        await adapter.connect()
    assert "did not complete within" in str(excinfo.value)


@pytest.mark.asyncio
async def test_concurrent_failed_connects_share_single_attempt(monkeypatch) -> None:
    """Concurrent failed cold connects share one attempt, not cumulative retries."""
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=0.05)
    attempts: list[int] = []

    class FakeClient:
        async def __aenter__(self):
            attempts.append(1)
            await asyncio.sleep(10)  # stall until the connect cap cancels
            return self

    monkeypatch.setattr("daimon_sdk._transport.Client", lambda *a, **k: FakeClient())

    results = await asyncio.gather(
        adapter.connect(), adapter.connect(), adapter.connect(), return_exceptions=True
    )
    assert len(attempts) == 1
    assert all(isinstance(r, DaimonConnectionError) for r in results)
    # A later caller after the shared failure retries with a fresh attempt.
    with pytest.raises(DaimonConnectionError):
        await adapter.connect()
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_connect_waiter_cancellation_does_not_cancel_shared_attempt(monkeypatch) -> None:
    """One waiter's cancellation must not cancel the shared connect for others."""
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=10.0)
    built: list[object] = []

    class FakeClient:
        async def __aenter__(self):
            await asyncio.sleep(0.05)
            return self

    def fake_client_ctor(transport, init_timeout=None):
        client = FakeClient()
        built.append(client)
        return client

    monkeypatch.setattr("daimon_sdk._transport.Client", fake_client_ctor)

    waiter_a = asyncio.ensure_future(adapter.connect())
    waiter_b = asyncio.ensure_future(adapter.connect())
    await asyncio.sleep(0.01)  # let both register on the shared connect task
    waiter_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_a
    # B still completes and the single shared client is established.
    await waiter_b
    assert len(built) == 1
    assert adapter._client is built[0]


@pytest.mark.asyncio
async def test_concurrent_close_shares_single_teardown() -> None:
    """Concurrent closers share one teardown; a later close waits for it."""
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=10.0)
    exits: list[int] = []

    class FakeClient:
        async def __aexit__(self, *args):
            await asyncio.sleep(0.05)  # slow teardown so a second close could interleave
            exits.append(1)

    adapter._client = FakeClient()  # type: ignore[assignment]

    await asyncio.gather(adapter.close(), adapter.close(), adapter.close())
    assert len(exits) == 1
    assert adapter._client is None

    # A subsequent close is a no-op.
    await adapter.close()
    assert len(exits) == 1


@pytest.mark.asyncio
async def test_connect_waits_for_in_progress_close(monkeypatch) -> None:
    """A connect during a close must wait for teardown, then connect fresh."""
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=10.0)
    exits: list[int] = []
    built: list[object] = []

    class OldClient:
        async def __aexit__(self, *args):
            await asyncio.sleep(0.05)  # slow teardown; connect must not slip in mid-way
            exits.append(1)

    class NewClient:
        async def __aenter__(self):
            built.append(self)
            return self

    monkeypatch.setattr("daimon_sdk._transport.Client", lambda *a, **k: NewClient())
    adapter._client = OldClient()  # type: ignore[assignment]

    close_future = asyncio.ensure_future(adapter.close())
    await asyncio.sleep(0.01)  # let close() acquire the lock and begin teardown
    await adapter.connect()  # blocks until teardown finishes, then connects fresh
    await close_future

    assert len(exits) == 1  # old client torn down exactly once
    assert len(built) == 1  # exactly one new connection, created after the close
    assert adapter._client is built[0]


@pytest.mark.asyncio
async def test_consume_connect_exception_marks_failure_retrieved() -> None:
    """The done callback consumes a failed attempt; it ignores cancelled tasks."""
    from daimon_sdk.exceptions import DaimonConnectionError as _DCE

    async def fail() -> None:
        raise _DCE("boom")

    failed = asyncio.ensure_future(fail())
    await asyncio.sleep(0.01)
    _consume_connect_exception(failed)
    # Retrieving again is safe because the callback already consumed it.
    assert isinstance(failed.exception(), _DCE)

    async def hang() -> None:
        await asyncio.sleep(10)

    cancelled = asyncio.ensure_future(hang())
    cancelled.cancel()
    await asyncio.sleep(0.01)
    _consume_connect_exception(cancelled)  # must not raise on a cancelled task
    assert cancelled.cancelled()


@pytest.mark.asyncio
async def test_connect_attaches_done_callback_consuming_exception(monkeypatch) -> None:
    """connect() must attach the consuming callback so a failed attempt with no
    surviving waiter is still marked retrieved (no "never retrieved" warning)."""
    adapter = FastMCPTransportAdapter("http://127.0.0.1:19000/mcp", access_token=None, timeout_s=0.05)

    class FakeClient:
        async def __aenter__(self):
            await asyncio.sleep(10)  # stall until the connect cap cancels it
            return self

    monkeypatch.setattr("daimon_sdk._transport.Client", lambda *a, **k: FakeClient())

    consumed: list[object] = []
    original = _consume_connect_exception

    def spy(task):
        consumed.append(task)
        original(task)

    monkeypatch.setattr("daimon_sdk._transport._consume_connect_exception", spy)

    waiter = asyncio.ensure_future(adapter.connect())
    await asyncio.sleep(0.01)  # let the shared attempt start
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    task = adapter._connect_task
    await asyncio.sleep(0.2)  # let the shared attempt fail and the callback fire
    assert task is not None and task.done()
    # Independent proof the callback was attached AND fired for this task.
    assert task in consumed
    assert isinstance(task.exception(), DaimonConnectionError)
