from loguru import logger

logger.disable("app")  # silent until the server calls logs.setup()
