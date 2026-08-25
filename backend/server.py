import asyncio

from server_app.routes import main
from server_app.config import log

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.warning("server stopped")
