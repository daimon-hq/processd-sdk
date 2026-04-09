from __future__ import annotations

import asyncio

from daimon_sdk import DaimonClient


async def main() -> None:
    async with DaimonClient("http://127.0.0.1:8080/mcp") as client:
        runtime = await client.runtime.get_context()
        print(runtime.summary)

        page = await client.web.fetch("https://example.com")
        print(page.status_code, page.result_type)


asyncio.run(main())
