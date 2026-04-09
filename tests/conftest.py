from __future__ import annotations

import socket
import os
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from daimon_sdk import DaimonClient

REPO_ROOT = Path(__file__).resolve().parents[1]
PROCESSD_ROOT = REPO_ROOT.parent / "processd-standalone"
PROCESSD_MCP_BIN = PROCESSD_ROOT / "target" / "debug" / "processd-mcp"
MCP_LOG = REPO_ROOT / ".pytest-daimon-sdk.log"
TEST_TOKEN = "test-token"


@dataclass
class ServerBundle:
    mcp_port: int
    token: str | None
    proc: subprocess.Popen[str]

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/mcp"

    @property
    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/health"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_for_http(url: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=0.5)
            if response.status_code in (200, 204):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}")


def _build_binary() -> None:
    subprocess.run(
        ["cargo", "build", "--bin", "processd-mcp"],
        cwd=PROCESSD_ROOT,
        check=True,
        text=True,
    )


def _start_server(token: str | None) -> ServerBundle:
    _build_binary()
    mcp_port = _find_free_port()
    full_env = os.environ.copy()
    full_env["MCP_PORT"] = str(mcp_port)
    if token:
        full_env["PROCESSD_TOKEN"] = token

    log_file = MCP_LOG.open("w")
    proc = subprocess.Popen(
        [str(PROCESSD_MCP_BIN)],
        cwd=PROCESSD_ROOT,
        env=full_env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_http(f"http://127.0.0.1:{mcp_port}/health")
    except Exception:
        proc.kill()
        proc.wait(timeout=5)
        raise
    return ServerBundle(mcp_port=mcp_port, token=token, proc=proc)


def _stop_server(bundle: ServerBundle) -> None:
    bundle.proc.kill()
    bundle.proc.wait(timeout=5)


@pytest.fixture(scope="session")
def unauth_server() -> ServerBundle:
    bundle = _start_server(None)
    try:
        yield bundle
    finally:
        _stop_server(bundle)


@pytest.fixture(scope="session")
def auth_server() -> ServerBundle:
    bundle = _start_server(TEST_TOKEN)
    try:
        yield bundle
    finally:
        _stop_server(bundle)


@pytest.fixture
async def client(unauth_server: ServerBundle) -> AsyncIterator[DaimonClient]:
    async with DaimonClient(unauth_server.mcp_url) as sdk_client:
        yield sdk_client


@pytest.fixture
async def auth_client(auth_server: ServerBundle) -> AsyncIterator[DaimonClient]:
    async with DaimonClient(auth_server.mcp_url, access_token=TEST_TOKEN) as sdk_client:
        yield sdk_client
