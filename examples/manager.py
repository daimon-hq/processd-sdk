import asyncio

from daimon_sdk import DaimonManagerClient


async def main() -> None:
    async with DaimonManagerClient("http://127.0.0.1:18080") as manager:
        capacity = await manager.capacity()
        print("manager capacity mode:", capacity.mode)

        async with manager.sandbox() as sandbox:
            context = await sandbox.runtime.get_context()
            print("workspace:", context.base_workdir)

            result = await sandbox.exec.bash("python3 --version")
            print(result.stdout or result.stderr)


if __name__ == "__main__":
    asyncio.run(main())
