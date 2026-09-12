import argparse
import asyncio
import logging
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from ledger.hold_expiration import expire_due_holds_in_session
from ledger.models.database import engine
from ledger.observability import HOLD_EXPIRATION_BATCHES, configure_logging
from ledger.settings import get_settings

logger = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Expire active balance holds whose expires_at is due."
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=settings.HOLD_EXPIRATION_INTERVAL_SECONDS,
        help="Seconds between expiration batches.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=settings.HOLD_EXPIRATION_BATCH_SIZE,
        help="Maximum holds expired per batch.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one expiration batch and exit.",
    )
    return parser.parse_args(argv)


async def expire_due_holds_once(*, batch_size: int | None = None) -> int:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        return await expire_due_holds_in_session(
            session,
            batch_size=batch_size,
        )


async def run_worker(
    *,
    interval_seconds: float,
    batch_size: int,
    once: bool = False,
) -> int:
    while True:
        try:
            expired_count = await expire_due_holds_once(
                batch_size=batch_size,
            )
            logger.info("Expired %s due holds", expired_count)
        except Exception:
            HOLD_EXPIRATION_BATCHES.labels(result="failure").inc()
            logger.exception("Hold expiration batch failed")
            expired_count = 0

        if once:
            return expired_count

        await asyncio.sleep(interval_seconds)


def main(argv: Sequence[str] | None = None) -> None:  # pragma: no cover
    configure_logging()
    args = parse_args(argv)
    asyncio.run(
        run_worker(
            interval_seconds=args.interval,
            batch_size=args.batch_size,
            once=args.once,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    main()
