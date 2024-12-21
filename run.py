import asyncio

from loguru import logger

from bot.main import start_bot

logger.add("bot/database/log.log",
           format="{time} {level} {message}",
           rotation="4 MB",
           compression="zip",
           diagnose=True
           )


async def main():
    s_bot = asyncio.create_task(start_bot())
    await asyncio.gather(s_bot)


if __name__ == '__main__':
    asyncio.run(main())
