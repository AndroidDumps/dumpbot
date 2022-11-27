import logging

from rich.logging import RichHandler

FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT, handlers=[RichHandler()])

logger = logging.getLogger("rich")
