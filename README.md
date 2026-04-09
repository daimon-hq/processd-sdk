# daimon-sdk

Typed async Python SDK for `processd-mcp`.

`daimon-sdk` wraps the raw MCP tool surface exposed by `processd-mcp` and presents it as grouped Python APIs such as `client.files.read()` and `client.exec.start_session()`. The SDK keeps `processd-standalone` as the contract source of truth and focuses on:

- connection and token wiring
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

## Release

Releases are published from GitHub Actions when a tag matching `v*` is pushed.

```bash
git tag v0.1.0
git push origin v0.1.0
```

The tag version must match `pyproject.toml`'s project version.
