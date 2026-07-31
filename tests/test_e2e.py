from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from daimon_sdk import (
    DaimonConnectionError,
    DaimonHttpError,
    DaimonManagerClient,
    DaimonToolError,
    NetworkPolicy,
    SandboxAction,
    SecretRule,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROCESSD_ROOT = REPO_ROOT.parent / "processd-standalone"
MANAGER_E2E_ENABLED = os.environ.get("PROCESSD_SDK_MANAGER_E2E") == "1"


class _QuietHTTPServer(HTTPServer):
    allow_reuse_address = True


class _WebFetchHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/page":
            body = b"<html><body><h1>SDK Test</h1><p>Hello <strong>web</strong></p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def _wait_for_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=0.5)
            if response.status_code in (200, 204):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {url}")


def _compose_down() -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(PROCESSD_ROOT / "compose.manager.yaml"),
            "-f",
            str(PROCESSD_ROOT / "compose.manager.cgroup.yaml"),
            "down",
            "--remove-orphans",
        ],
        cwd=PROCESSD_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )


def _manager_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PROCESSD_MANAGER_CGROUP_MEMORY_MAX_BYTES", "536870912")
    env.setdefault("PROCESSD_MANAGER_CGROUP_SWAP_MAX_BYTES", "0")
    env.setdefault("PROCESSD_MANAGER_CGROUP_PIDS_MAX", "128")
    env.setdefault("PROCESSD_MANAGER_CGROUP_CPU_MS_PER_SEC", "500")
    if extra:
        env.update(extra)
    return env


@contextmanager
def _start_docker_manager(*, extra_env: dict[str, str] | None = None) -> Iterator[str]:
    assert PROCESSD_ROOT.exists(), f"missing sibling processd-standalone at {PROCESSD_ROOT}"
    _compose_down()
    subprocess.run(
        ["make", "docker-up-manager"],
        cwd=PROCESSD_ROOT,
        env=_manager_env(extra_env),
        check=True,
        text=True,
    )
    manager_url = "http://127.0.0.1:18080"
    _wait_for_http(f"{manager_url}/health")
    try:
        yield manager_url
    finally:
        _compose_down()


@pytest.fixture
def docker_manager_url() -> Iterator[str]:
    with _start_docker_manager() as manager_url:
        yield manager_url


@pytest.mark.asyncio
async def test_runtime_and_raw_tool_access(client) -> None:
    runtime = await client.runtime.get_context()
    assert runtime.base_workdir
    raw = await client.raw.call_tool("GetRuntimeContext", {})
    assert raw["baseWorkdir"] == runtime.base_workdir


@pytest.mark.asyncio
async def test_files_roundtrip_and_search(client, tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\ngamma\n")

    read = await client.files.read(str(target))
    assert read.kind == "text"
    assert "alpha" in read.file.content

    edited = await client.files.edit(str(target), old_string="beta", new_string="delta")
    assert edited.file_path == str(target)

    written = await client.files.write(str(target), "rewritten\n")
    assert written.type == "update"

    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "a.txt").write_text("alpha\n")
    glob = await client.files.glob("**/*.txt", path=str(tmp_path))
    assert glob.search_path == str(tmp_path)
    assert glob.num_files >= 2

    grep = await client.files.grep("alpha", path=str(tmp_path), output_mode="content")
    assert grep.num_files >= 1
    assert grep.content is not None

    uploaded = await client.files.upload_bytes(str(tmp_path / "binary" / "blob.bin"), b"\x00\x01\x02")
    assert uploaded.bytes_written == 3
    assert uploaded.created_parent_directories

    downloaded = await client.files.download_bytes(str(tmp_path / "binary" / "blob.bin"))
    assert downloaded == b"\x00\x01\x02"

    async def stream_chunks():
        yield b"stream-"
        yield b"upload"

    streamed = await client.files.upload_stream(
        str(tmp_path / "binary" / "stream.bin"),
        stream_chunks(),
    )
    assert streamed.bytes_written == len(b"stream-upload")
    streamed_chunks = [
        chunk
        async for chunk in client.files.download_stream(
            str(tmp_path / "binary" / "stream.bin")
        )
    ]
    assert b"".join(streamed_chunks) == b"stream-upload"

    local_copy = await client.files.download_file(
        str(tmp_path / "binary" / "blob.bin"),
        tmp_path / "downloaded" / "copy.bin",
    )
    assert local_copy.read_bytes() == b"\x00\x01\x02"


@pytest.mark.asyncio
async def test_read_and_write_errors_raise_daimon_tool_error(client, tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    with pytest.raises(DaimonToolError):
        await client.files.read(str(missing))
    with pytest.raises(DaimonHttpError) as exc_info:
        await client.files.download_bytes(str(missing))
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_web_fetch_and_bash(client) -> None:
    bash = await client.exec.bash("printf 'hello bash\\n'")
    assert bash.stdout == "hello bash\n"

    server = _QuietHTTPServer(("127.0.0.1", 0), _WebFetchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        web = await client.web.fetch(f"http://127.0.0.1:{server.server_address[1]}/page")
        assert web.status_code == 200
        assert web.result_type == "text"
        assert "# SDK Test" in web.content
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_exec_session_lifecycle(client) -> None:
    session = await client.exec.start_session("/bin/cat", tty=True, yield_time_ms=100)
    echoed = await session.write("hello session\n", yield_time_ms=100)
    assert "hello session" in echoed.output

    exited = await session.close(exit_payload="\u0004", yield_time_ms=500)
    assert exited.exit_code == 0


@pytest.mark.asyncio
async def test_background_bash_and_auth(auth_client) -> None:
    secure = await auth_client.exec.exec_command("echo secure", yield_time_ms=200)
    assert secure.exit_code == 0
    assert secure.output.strip() == "secure"

    bg = await auth_client.exec.bash(
        "printf 'start\\n'; sleep 0.2; printf 'end\\n'",
        run_in_background=True,
    )
    assert bg.background_task_id
    output_path = bg.persisted_output_path
    assert output_path

    deadline = time.monotonic() + 5
    final_text = None
    while time.monotonic() < deadline:
        final = await auth_client.files.read(output_path)
        final_text = final.file.content
        if "[process exited with code 0]" in final_text:
            break
        await asyncio.sleep(0.1)

    assert final_text is not None
    assert "start" in final_text
    assert "end" in final_text


@pytest.mark.asyncio
@pytest.mark.skipif(not MANAGER_E2E_ENABLED, reason="set PROCESSD_SDK_MANAGER_E2E=1")
async def test_docker_manager_ttl_zero_expires_on_next_reaper_loop() -> None:
    with _start_docker_manager(
        extra_env={"PROCESSD_MANAGER_REAPER_INTERVAL_SECONDS": "1"}
    ) as manager_url:
        async with DaimonManagerClient(manager_url) as manager:
            sandbox = await manager.create_sandbox()
            updated = await sandbox.set_ttl(0)
            assert updated.ttl_seconds == 0

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    await manager.get_sandbox(sandbox.id)
                except DaimonHttpError as exc:
                    assert exc.status_code == 404
                    break
                except DaimonConnectionError:
                    pass
                await asyncio.sleep(1)
            else:
                raise AssertionError("sandbox was not reaped after ttl_seconds=0")


@pytest.mark.manager_e2e
@pytest.mark.skipif(
    not MANAGER_E2E_ENABLED,
    reason="set PROCESSD_SDK_MANAGER_E2E=1 to run Docker manager SDK E2E",
)
@pytest.mark.asyncio
async def test_docker_manager_lifecycle_and_sandbox_mcp(docker_manager_url: str) -> None:
    async with DaimonManagerClient(docker_manager_url) as manager:
        assert await manager.health()
        capacity = await manager.capacity()
        assert capacity.mode == "resource"
        assert capacity.memory_bytes.capacity is not None
        assert capacity.pids.capacity is not None
        assert capacity.cpu_ms_per_sec.capacity is not None

        sandbox = await manager.create_sandbox()
        try:
            assert sandbox.info.state == "running"
            await sandbox.connect()
            context = await sandbox.runtime.get_context()
            assert context.base_workdir == "/workspace"
            result = await sandbox.exec.bash("echo hello")
            assert result.stdout == "hello\n"

            stopped = await sandbox.stop()
            assert stopped.state == "stopped"
            with pytest.raises(Exception):
                _wait_for_http(sandbox.info.mcp_url.replace("/mcp", "/health"), timeout=1)

            restarted = await sandbox.start()
            assert restarted.state == "running"
            _wait_for_http(sandbox.info.mcp_url.replace("/mcp", "/health"), timeout=10)
        finally:
            await sandbox.delete()

        with pytest.raises(DaimonHttpError) as exc_info:
            await manager.get_sandbox(sandbox.id)
        assert exc_info.value.status_code == 404


@pytest.mark.manager_e2e
@pytest.mark.skipif(
    not MANAGER_E2E_ENABLED,
    reason="set PROCESSD_SDK_MANAGER_E2E=1 to run Docker manager SDK E2E",
)
@pytest.mark.asyncio
async def test_docker_manager_context_manager_deletes_sandbox(docker_manager_url: str) -> None:
    async with DaimonManagerClient(docker_manager_url) as manager:
        async with manager.sandbox() as sandbox:
            sandbox_id = sandbox.id
            result = await sandbox.exec.bash("pwd")
            assert result.stdout.strip() == "/workspace"

        with pytest.raises(DaimonHttpError) as exc_info:
            await manager.get_sandbox(sandbox_id)
        assert exc_info.value.status_code == 404


@pytest.mark.manager_e2e
@pytest.mark.skipif(
    not MANAGER_E2E_ENABLED,
    reason="set PROCESSD_SDK_MANAGER_E2E=1 to run Docker manager SDK E2E",
)
@pytest.mark.asyncio
async def test_docker_manager_admission_429_payload_is_preserved() -> None:
    with _start_docker_manager(
        extra_env={
            "PROCESSD_MANAGER_SANDBOX_REQUEST_MEMORY_BYTES": str(1 << 62),
            "PROCESSD_MANAGER_SANDBOX_REQUEST_PIDS": "0",
            "PROCESSD_MANAGER_SANDBOX_REQUEST_CPU_MS_PER_SEC": "0",
        }
    ) as manager_url:
        async with DaimonManagerClient(manager_url) as manager:
            with pytest.raises(DaimonHttpError) as exc_info:
                await manager.create_sandbox()
            assert exc_info.value.status_code == 429
            assert exc_info.value.payload["admission"]["missing"][0]["resource"] == "memory_bytes"


@pytest.mark.manager_e2e
@pytest.mark.skipif(
    not MANAGER_E2E_ENABLED,
    reason="set PROCESSD_SDK_MANAGER_E2E=1 to run Docker manager SDK E2E",
)
@pytest.mark.asyncio
async def test_docker_manager_sandbox_egress_proxy(docker_manager_url: str) -> None:
    async with DaimonManagerClient(docker_manager_url) as manager:
        # Pure-list semantics: the proxy dials whatever an allowed hostname
        # resolves to without filtering, so hosts whose resolver answers with
        # fake-IP ranges (e.g. a VPN's 198.18.0.0/15) work with no extra knob.
        policy = NetworkPolicy.proxy(["example.com"])
        sandbox = await manager.create_sandbox(network_policy=policy)
        try:
            echoed = sandbox.info.network_policy
            assert echoed is not None
            assert echoed.mode == "proxy"
            assert echoed.allow == ["example.com"]
            # No allow_ports restriction: every port is tunneled.
            assert echoed.allow_ports is None

            await sandbox.connect()

            # The manager injects per-sandbox proxy env pointing at the
            # loopback egress proxy.
            env = await sandbox.exec.bash(
                "printf '%s|%s' \"$HTTP_PROXY\" \"$NO_PROXY\"",
                timeout_ms=30_000,
            )
            assert "http://127.0.0.1:" in env.stdout
            assert "localhost,127.0.0.1,::1" in env.stdout

            # An allow-listed domain is tunneled through the proxy.
            allowed = await sandbox.exec.bash(
                "curl -sS -o /dev/null -w '%{http_code}' --max-time 30 https://example.com",
                timeout_ms=60_000,
            )
            assert "200" in allowed.stdout

            # A non-listed domain is denied: curl reports the CONNECT 403
            # and the transfer itself yields http_code 000.
            denied = await sandbox.exec.bash(
                "curl -sS -o /dev/null -w '%{http_code}' --max-time 30 https://www.iana.org 2>&1",
                timeout_ms=60_000,
            )
            denied_out = denied.stdout + denied.stderr
            assert "403" in denied_out
            assert "200" not in denied_out

            # Bypassing the proxy is impossible: proxy mode runs pasta
            # splice-only, so the sandbox has no interface, route, or DNS and
            # direct connections fail fast.
            direct = await sandbox.exec.bash(
                "curl -sS --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 10"
                " https://example.com 2>&1; echo rc=$?",
                timeout_ms=30_000,
            )
            direct_out = direct.stdout + direct.stderr
            assert "200" not in direct_out
            assert "rc=0" not in direct_out
        finally:
            await sandbox.delete()

        # An empty allow list blocks all egress, while inbound
        # manager->sandbox MCP keeps working (the bash call itself succeeds).
        blocked_id = None
        async with manager.sandbox(network_policy=NetworkPolicy.proxy([])) as blocked:
            blocked_id = blocked.id
            result = await blocked.exec.bash(
                "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 https://example.com 2>&1",
                timeout_ms=30_000,
            )
            blocked_out = result.stdout + result.stderr
            assert "403" in blocked_out
            assert "200" not in blocked_out

        with pytest.raises(DaimonHttpError) as exc_info:
            await manager.get_sandbox(blocked_id)
        assert exc_info.value.status_code == 404


@pytest.mark.manager_e2e
@pytest.mark.skipif(
    not MANAGER_E2E_ENABLED,
    reason="set PROCESSD_SDK_MANAGER_E2E=1 to run Docker manager SDK E2E",
)
@pytest.mark.asyncio
async def test_docker_manager_find_or_create_network_policy_mismatch(docker_manager_url: str) -> None:
    async with DaimonManagerClient(docker_manager_url) as manager:
        labels = {"sdk-e2e": "network-policy"}
        policy = NetworkPolicy.proxy(["example.com"])
        sandbox = await manager.find_or_create_sandbox(labels=labels, network_policy=policy)
        try:
            assert sandbox.info.action == SandboxAction.CREATED

            # An equivalent policy reuses the sandbox (duplicates are
            # canonicalized away by the manager).
            equivalent = NetworkPolicy.proxy(["example.com", "example.com"])
            reused = await manager.find_or_create_sandbox(labels=labels, network_policy=equivalent)
            assert reused.info.action == SandboxAction.REUSED
            assert reused.id == sandbox.id

            # A different effective policy is refused with a 400 rather than
            # silently reusing a sandbox with different egress.
            with pytest.raises(DaimonHttpError) as exc_info:
                await manager.find_or_create_sandbox(
                    labels=labels,
                    network_policy=NetworkPolicy.allow_all(),
                )
            assert exc_info.value.status_code == 400
        finally:
            await sandbox.delete()


@pytest.mark.manager_e2e
@pytest.mark.skipif(
    not MANAGER_E2E_ENABLED,
    reason="set PROCESSD_SDK_MANAGER_E2E=1 to run Docker manager SDK E2E",
)
@pytest.mark.asyncio
async def test_docker_manager_sandbox_secret_substitution(docker_manager_url: str) -> None:
    async with DaimonManagerClient(docker_manager_url) as manager:
        policy = NetworkPolicy.proxy(
            ["httpbin.org", "example.com"],
            secrets={
                "SDK_TEST_KEY": SecretRule(
                    value="sdk-real-secret",
                    allowed_hosts=["httpbin.org"],
                ),
                # Substituted only on example.com, so httpbin must see this
                # one's placeholder verbatim.
                "SDK_OTHER_KEY": SecretRule(
                    value="sdk-other-secret",
                    allowed_hosts=["example.com"],
                ),
            },
        )
        sandbox = await manager.create_sandbox(network_policy=policy)
        try:
            echoed = sandbox.info.network_policy
            assert echoed is not None
            # The API redacts the secret value but echoes the placeholder.
            rule = echoed.secrets["SDK_TEST_KEY"]
            assert rule.value == "***"
            assert rule.placeholder is not None
            assert rule.placeholder.startswith("pdm-vlt-")

            await sandbox.connect()

            # The jail env carries only the placeholder, never the value.
            env = await sandbox.exec.bash(
                "printf '%s' \"$SDK_TEST_KEY\"", timeout_ms=30_000
            )
            assert env.stdout.startswith("pdm-vlt-")
            assert "sdk-real-secret" not in env.stdout

            # Header substitution against an allowed host: httpbin echoes the
            # real value the proxy injected.
            headers = await sandbox.exec.bash(
                "curl -sS --max-time 30 -H \"Authorization: Bearer $SDK_TEST_KEY\""
                " https://httpbin.org/headers",
                timeout_ms=60_000,
            )
            assert "sdk-real-secret" in headers.stdout
            assert "pdm-vlt-" not in headers.stdout

            # Body substitution against the same host.
            body = await sandbox.exec.bash(
                "curl -sS --max-time 30 -X POST -d '{\"key\":\"'$SDK_TEST_KEY'\"}'"
                " https://httpbin.org/post",
                timeout_ms=60_000,
            )
            assert "sdk-real-secret" in body.stdout

            # A host NOT in a secret's allowed_hosts receives the placeholder
            # verbatim — httpbin echoes it back unsubstituted.
            passthrough = await sandbox.exec.bash(
                "curl -sS --max-time 30 -H \"X-Other: $SDK_OTHER_KEY\""
                " https://httpbin.org/headers",
                timeout_ms=60_000,
            )
            assert "pdm-vlt-" in passthrough.stdout
            assert "sdk-other-secret" not in passthrough.stdout

            # Non-allowed hosts stay reachable (the placeholder is useless
            # without the proxy's mapping, so this is safe by design).
            other = await sandbox.exec.bash(
                "curl -sS --max-time 30 -H \"X-Key: $SDK_TEST_KEY\""
                " -o /dev/null -w '%{http_code}' https://example.com; echo rc=$?",
                timeout_ms=60_000,
            )
            assert "200" in other.stdout
            assert "rc=0" in other.stdout
        finally:
            await sandbox.delete()
