import asyncio
import logging
import sys

from .bot import main


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("👋 Завершение работы по запросу пользователя")
        sys.exit(0)
    except Exception as error:
        logging.error("❌ Необработанная ошибка: %s", error, exc_info=True)
        sys.exit(1)
