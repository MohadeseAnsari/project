"""Persistent sender Worker that publishes processed articles to channels.

A separate process from the generation Worker (``app.worker.worker``) on
purpose, for three reasons:

1. Publishing is throttled by ``sender_send_delay_seconds`` between messages.
   Keeping that delay out of the generation loop means slow or rate-limited
   channels never slow down article generation.
2. A platform outage can fail thousands of sends without touching a single LLM
   call, so the two failure domains stay separate.
3. A human confirmation step is planned before publishing to a channel. When it
   lands, it changes only this loop — the ``awaiting_confirmation`` delivery
   status and the compose service are already in place.

Each iteration: recover abandoned sends, queue deliveries for newly processed
articles, claim the oldest due delivery, send it, then wait the configured
delay before the next send so the channel never sees a burst. The loop survives
individual failures and empty polls; only SIGTERM/SIGINT stop it, after the
in-flight send completes.
"""

import asyncio
import logging
import signal

from app.config import settings
from app.db.connection import async_session
from app.messaging.dispatcher import (
    claim_next_delivery,
    deliver_one,
    ensure_pending_deliveries,
    recover_stale_deliveries,
)
from app.messaging.platforms import configured_clients

logger = logging.getLogger(__name__)


async def _wait(stop_event: asyncio.Event, seconds: float) -> None:
    """Sleep up to ``seconds``, returning early if shutdown is requested.

    Lets a graceful stop interrupt the inter-send delay instead of waiting it
    out.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_sender() -> None:
    """Run the sender loop until a shutdown signal is received."""
    logger.info("Sender worker started")
    stop_event = asyncio.Event()

    def signal_handler():
        """Stop after the in-flight send (recovery re-queues it if cut short)."""
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    clients = configured_clients()
    if not clients:
        logger.warning("No messaging platform tokens configured; nothing will be sent")

    while not stop_event.is_set():
        try:
            async with async_session() as session:
                await recover_stale_deliveries(session)
                await ensure_pending_deliveries(session)

                delivery = await claim_next_delivery(session)
                if delivery is None:
                    # Queue empty: poll slowly rather than spinning.
                    await _wait(stop_event, settings.sender_poll_interval_seconds)
                    continue

                await deliver_one(session, delivery, clients)

            # Applied after every send attempt, successful or not, so a channel
            # is never hit faster than the configured delay.
            await _wait(stop_event, settings.sender_send_delay_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Sender loop error: %s", e)
            await _wait(stop_event, settings.sender_poll_interval_seconds)

    logger.info("Sender worker stopped")


def main() -> None:
    """Console entry point: configure logging and run the sender loop."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs one INFO line per request at the full request URL, and every
    # platform call puts the bot token in that URL. Silence httpx so the token
    # never reaches the logs — the same "never logged" rule the shared secret
    # and the LLM key follow. Our own INFO lines cover the same events without
    # the credential.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run_sender())


if __name__ == "__main__":
    main()
