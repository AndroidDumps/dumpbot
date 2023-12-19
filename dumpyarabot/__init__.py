import logging

from rich.logging import RichHandler

FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT, handlers=[RichHandler()])
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("rich")
