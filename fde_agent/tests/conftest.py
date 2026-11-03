"""Hide the store's DEBUG logs so test failures are readable."""
try:
    from loguru import logger
    logger.remove()
    logger.add(lambda msg: None, level="WARNING")
except ImportError:
    pass
