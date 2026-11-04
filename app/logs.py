"""Logging: everything goes through loguru, including uvicorn and library messages.

  console                          LOG_LEVEL and above (default INFO)
  runtime/logs/invoice-flow.log    DEBUG and above, rotated at 10 MB, last 5 kept

Importing `app` logs nothing (see app/__init__.py) so tests and scripts keep their
own output; the server turns logging on by calling setup().
"""
import logging
import sys

from loguru import logger

from . import config

CONSOLE_FORMAT = "<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> <cyan>{extra[run]: <8}</cyan> <level>{message}</level>"
FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[run]: <8} | {name}:{function}:{line} - {message}"


class _Intercept(logging.Handler):
    """Forward stdlib logging records (uvicorn, pdfminer, httpx...) to loguru."""

    def emit(self, record):
        if record.name == "asyncio" and record.exc_info and isinstance(record.exc_info[1], ConnectionResetError):
            return  # Windows reports every browser tab that drops its live connection this way; it isn't a fault
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        origin = dict(name=record.name, function=record.funcName, line=record.lineno)
        logger.patch(lambda r: r.update(origin)).opt(exception=record.exc_info).log(level, record.getMessage())


def setup():
    logger.remove()
    logger.configure(extra={"run": "-"})  # default for lines outside a run; the worker fills in the run id
    # diagnose=False: tracebacks must not print variable values (invoice contents, API keys)
    logger.add(sys.stderr, level=config.LOG_LEVEL, format=CONSOLE_FORMAT, diagnose=False)
    logger.add(config.LOG_DIR / "invoice-flow.log", level="DEBUG", format=FILE_FORMAT, diagnose=False,
               rotation="10 MB", retention=5, encoding="utf-8")
    logging.basicConfig(handlers=[_Intercept()], level=logging.WARNING, force=True)
    logger.enable("app")
