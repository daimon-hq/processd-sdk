from __future__ import annotations

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from daimon_sdk import DaimonToolError


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


@pytest.mark.asyncio
async def test_read_and_write_errors_raise_daimon_tool_error(client, tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    with pytest.raises(DaimonToolError):
        await client.files.read(str(missing))


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
