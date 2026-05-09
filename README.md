# daimon-sdk

Typed async Python SDK for `processd-mcp` and `processd-sandbox-manager`.

`daimon-sdk` wraps the raw MCP tool surface exposed by `processd-mcp` and presents it as grouped Python APIs such as `client.files.read()` and `client.exec.start_session()`. The SDK keeps `processd-standalone` as the contract source of truth and focuses on:

- connection and token wiring
- manager sandbox lifecycle helpers
- typed request/response handling
- structured tool error mapping
- interactive session helpers
- compatibility tests against a real `processd-mcp` binary

## Install

```bash
pip install daimon-sdk
```

For local development:

```bash
pip install -e ".[dev]"
```

## Quickstart

With a sandbox manager:

```python
import asyncio

from daimon_sdk import DaimonManagerClient


async def main() -> None:
    async with DaimonManagerClient("http://127.0.0.1:18080") as manager:
        async with manager.sandbox() as sandbox:
            runtime = await sandbox.runtime.get_context()
            print(runtime.base_workdir)

            result = await sandbox.exec.bash("python3 --version")
            print(result.stdout or result.stderr)


asyncio.run(main())
```

If you already have a sandbox MCP URL and token, connect directly to MCP:

```python
import asyncio

from daimon_sdk import DaimonClient


async def main() -> None:
    async with DaimonClient("http://127.0.0.1:8080/mcp") as client:
        runtime = await client.runtime.get_context()
        print(runtime.base_workdir)

        result = await client.files.glob("**/*.rs", path=runtime.base_workdir)
        print(result.filenames[:5])

        bash = await client.exec.bash("printf 'hello from processd\\n'")
        print(bash.stdout)


asyncio.run(main())
```

## Raw MCP vs SDK

Raw MCP:

```python
payload = await mcp_client.call_tool("Read", {"file_path": "/tmp/demo.txt"})
```

SDK:

```python
read = await client.files.read("/tmp/demo.txt")
print(read.file.content)
```

## API Overview

- `DaimonClient(base_url, access_token=None, timeout_s=30.0)`
- `await client.connect()` / `await client.close()`
- `async with DaimonClient(...) as client`
- `client.runtime.get_context()`
- `client.files.read() / write() / edit() / glob() / grep()`
- `client.exec.bash() / start_session()`
- `SessionHandle.write() / poll() / wait_for_exit() / close()`
- `client.web.fetch()`
- `client.raw.call_tool()`
- `DaimonManagerClient(base_url, access_token=None, timeout_s=30.0)`
- `await manager.health()`
- `await manager.capacity()`
- `await manager.create_sandbox()`
- `await manager.find_or_create_sandbox(labels={"thread_id": thread_id})`
- `await manager.get_sandbox(id)`
- `await manager.start_sandbox(id) / stop_sandbox(id) / delete_sandbox(id)`
- `await manager.update_sandbox(id, ttl_seconds=...)`
- `async with manager.sandbox() as sandbox`
- `await sandbox.set_ttl(ttl_seconds)`
- `sandbox.runtime/files/exec/web/raw`

`manager.sandbox()` creates a sandbox, connects to its MCP endpoint, and deletes
it on context exit by default. Use `delete_on_exit=False` or
`create_sandbox()` when the workspace should survive beyond the context.
`ttl_seconds=0` marks a sandbox as immediately expired so the next manager
reaper loop deletes it.

## Local Testing

The SDK compatibility tests expect a sibling checkout of `processd-standalone`:

```text
e2b-project/
  processd-standalone/
  processd-sdk/
```

Run tests with an environment that already has the dev dependencies installed:

```bash
PYTHONPATH=src python -m pytest -q
```

The E2E suite builds and launches `../processd-standalone/target/debug/processd-mcp`.

Manager E2E uses the sibling `processd-standalone` Docker manager compose path:

```bash
PROCESSD_SDK_MANAGER_E2E=1 PYTHONPATH=src python -m pytest -q -m manager_e2e
```

## Release

Releases are published from GitHub Actions when a tag matching `v*` is pushed.

```bash
git tag v0.4.0
git push origin v0.4.0
```

The tag version must match `pyproject.toml`'s project version.
