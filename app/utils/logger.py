import logging


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy third-party loggers so the console stays readable.
    for noisy in ("httpx", "apscheduler.executors.default", "telegram.ext.Updater"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
