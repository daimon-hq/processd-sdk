from __future__ import annotations

import threading
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from daimon_sdk._transport import decode_tool_result
from daimon_sdk.exceptions import DaimonHttpError, DaimonProtocolError
from daimon_sdk.manager import DaimonSandbox, ManagerHTTPTransport
from daimon_sdk.models import (
    ExecResult,
    ManagerCapacityResult,
    SandboxInfo,
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
    "limits": {
        "rlimit": "applied",
        "cgroup": "applied",
        "cgroup_reason": None,
    },
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


def test_decode_tool_result_raises_for_invalid_text_json() -> None:
    with pytest.raises(DaimonProtocolError):
        decode_tool_result(DummyResult(content=[DummyText("not-json")]))


def test_manager_models_parse_payloads() -> None:
    sandbox = SandboxInfo.from_dict(SANDBOX_PAYLOAD)
    assert sandbox.id == "sandbox-1"
    assert sandbox.limits.rlimit == "applied"
    assert sandbox.labels["thread_id"] == "thread-a"
    assert sandbox.ttl_seconds == 3600
    assert sandbox.expires_at == 3724
    assert sandbox.raw_payload["mcp_url"] == SANDBOX_PAYLOAD["mcp_url"]

    capacity = ManagerCapacityResult.from_dict(CAPACITY_PAYLOAD)
    assert capacity.mode == "resource"
    assert capacity.memory_bytes.capacity == 8589934592
    assert capacity.cpu_ms_per_sec.available == 2500
    assert capacity.raw_payload["capacity_source"] == "/sys/fs/cgroup/test"


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


@pytest.mark.asyncio
async def test_manager_sandbox_context_deletes_on_exception(monkeypatch) -> None:
    from daimon_sdk.manager import DaimonManagerClient, DaimonSandbox

    calls: list[str] = []

    async def fake_create(self):
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

    async def fake_create(self):
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
