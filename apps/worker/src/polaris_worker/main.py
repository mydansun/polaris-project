import argparse
import asyncio
import logging
import os

from polaris_worker import __version__
from polaris_worker.config import get_settings
from polaris_worker.runner import run_worker


def main() -> None:
    parser = argparse.ArgumentParser(description="Polaris background worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    print(f"polaris-worker {__version__}: starting")
    asyncio.run(run_worker(get_settings(), once=args.once))


if __name__ == "__main__":
    main()
