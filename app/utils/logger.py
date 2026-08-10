import logging


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy third-party loggers so the console stays readable.
    for noisy in ("httpx", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # PTB already retries long-polling network failures. Its default updater logger
    # prints a full traceback for every DNS retry, which obscures real application errors.
    logging.getLogger("telegram.ext.Updater").setLevel(logging.CRITICAL)
