from __future__ import annotations

import asyncio
from pathlib import Path

from daimon_sdk import DaimonClient


async def main() -> None:
    target = Path("/tmp/daimon-sdk-demo.txt")
    async with DaimonClient("http://127.0.0.1:8080/mcp") as client:
        await client.files.edit(str(target), old_string="", new_string="hello from sdk\n")
        read = await client.files.read(str(target))
        print(read.file.content)
        glob = await client.files.glob("*.txt", path=str(target.parent))
        print(glob.filenames)


asyncio.run(main())
