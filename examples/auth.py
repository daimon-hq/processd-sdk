from __future__ import annotations

import asyncio
import os

from daimon_sdk import DaimonClient


async def main() -> None:
    token = os.environ["PROCESSD_TOKEN"]
    async with DaimonClient("http://127.0.0.1:8080/mcp", access_token=token) as client:
        result = await client.exec.exec_command("echo secure", yield_time_ms=200)
        print(result.output.strip())


asyncio.run(main())
