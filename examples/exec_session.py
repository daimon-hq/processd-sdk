from __future__ import annotations

import asyncio

from daimon_sdk import DaimonClient


async def main() -> None:
    async with DaimonClient("http://127.0.0.1:8080/mcp") as client:
        session = await client.exec.start_session("/bin/cat", tty=True, yield_time_ms=100)
        echoed = await session.write("hello session\n", yield_time_ms=100)
        print(echoed.output)
        exited = await session.close(exit_payload="\u0004", yield_time_ms=500)
        print(exited.exit_code)


asyncio.run(main())
