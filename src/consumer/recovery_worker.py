"""Separate polling process for the safely repeatable demo schedule feature."""

import argparse
import logging
import math
import signal
from threading import Event

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..shared.database import create_database_engine
from ..shared.models import APIFeature
from ..shared.quota import claim_expired_reservations
from .service import DemoFeatureFailure, execute_schedule_operation


logger = logging.getLogger(__name__)


def recover_pending_operations(session: Session, *, limit: int = 20) -> int:
    operations = claim_expired_reservations(
        session, feature=APIFeature.SAILING_SCHEDULE, limit=limit
    )
    settled = 0
    for operation in operations:
        try:
            execute_schedule_operation(session, operation)
        except DemoFeatureFailure:
            # The shared execution path already confirmed failure and released it.
            settled += 1
            logger.info("Released operation=%s claim=%s", operation.id, operation.claim_version)
        except Exception as exc:
            # Includes invalid/missing context, stale claims and uncertain database
            # outcomes. No blind refund or automatic handling of external effects.
            session.rollback()
            logger.warning(
                "Unresolved operation=%s claim=%s error_type=%s; no automatic refund",
                operation.id, operation.claim_version, type(exc).__name__,
            )
        else:
            settled += 1
            logger.info("Completed operation=%s claim=%s", operation.id, operation.claim_version)
    return settled


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process one bounded batch and exit")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--batch-size", type=int, default=20)
    arguments = parser.parse_args()
    if not math.isfinite(arguments.poll_interval) or arguments.poll_interval <= 0:
        parser.error("--poll-interval must be finite and positive")
    if arguments.batch_size <= 0:
        parser.error("--batch-size must be positive")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    stopped = Event()

    def stop(signum, frame):
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    engine = create_database_engine()
    try:
        while not stopped.is_set():
            try:
                with Session(engine) as session:
                    settled = recover_pending_operations(session, limit=arguments.batch_size)
                logger.info("Recovery poll settled=%s", settled)
            except SQLAlchemyError:
                logger.warning("Recovery database unavailable; no automatic refunds")
                if arguments.once:
                    return 1
            if arguments.once:
                break
            stopped.wait(arguments.poll_interval)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
