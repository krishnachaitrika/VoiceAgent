"""
billing/twilio_reconcile.py — replace estimated Twilio cost with the real
amount Twilio billed.

THE PROBLEM WITH ESTIMATES

api/websocket.py computes Twilio cost as `ceil(duration/60) x configured_rate`.
That is only as good as the rate someone typed into .env, and it drifts in
three ways nobody notices:

  1. The rate is a guess until checked against an invoice. If outbound to an
     Indian mobile is really $0.09/min and .env says $0.12, every outbound
     call is 33% overstated — and the figure still looks perfectly credible.
  2. Twilio prices by DESTINATION. One rate cannot cover a second market, so
     the day you dial a different country the numbers quietly go wrong.
  3. Provider pricing changes. Nothing tells you; the estimate just becomes
     wrong from that day forward.

THE FIX

Twilio's Call resource exposes `price` — the actual amount charged, in the
account's currency. Fetching it turns the Twilio column from a calculation
into a reconciled fact, with no rate to maintain and no per-country table.

WHY IT IS NOT IMMEDIATE

`price` is null when a call ends and is populated once Twilio finishes rating
it — usually seconds, occasionally longer. So this runs as a delayed
background reconciliation:

    call ends  ->  estimate written immediately (dashboard is never blank)
               ->  wait, fetch the real price, overwrite, mark as "actual"

`twilio_cost_source` records which of the two any given row holds, so a
reconciliation that never succeeded is visible rather than silently passing as
measured.

DESIGN NOTES

  - httpx, not the twilio SDK. The SDK's client is synchronous and would block
    the event loop — the same class of bug as the Google Calendar call in
    brain/tools.py, and just as invisible until you have concurrent traffic.
  - Never raises into the call path. A failed reconciliation leaves the
    estimate in place; it must not affect the caller or the call record.
  - Task references are retained. asyncio holds only a weak reference to a
    bare create_task, so a task nothing keeps a handle on can be collected
    before it runs.
"""
import asyncio
import logging
from typing import Optional, Tuple

import httpx

import config
from database import crud
from database.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

_TWILIO_API = "https://api.twilio.com/2010-04-01"

# Strong refs to in-flight reconciliations so they survive until completion.
_pending: set = set()


async def fetch_call_price(call_sid: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Fetch the actual billed price for one call.

    Returns (price_usd_positive, currency) or (None, None) if unavailable.

    Twilio reports `price` as a NEGATIVE string ("-0.24000") because it is a
    debit against the account balance. It is normalised to a positive number
    here so it can be summed with the other provider costs without a sign trap
    in every query that touches it.
    """
    if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN):
        return None, None

    url = f"{_TWILIO_API}/Accounts/{config.TWILIO_ACCOUNT_SID}/Calls/{call_sid}.json"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            resp = await client.get(
                url, auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.warning(
            f"[{call_sid}] Twilio price fetch returned {e.response.status_code} — "
            f"keeping the estimate"
        )
        return None, None
    except Exception as e:
        logger.warning(f"[{call_sid}] Twilio price fetch failed ({e}) — keeping the estimate")
        return None, None

    raw_price = data.get("price")
    if raw_price is None:
        # Not yet rated. The caller retries.
        return None, None

    try:
        price = abs(float(raw_price))
    except (TypeError, ValueError):
        logger.warning(f"[{call_sid}] Twilio returned an unparseable price: {raw_price!r}")
        return None, None

    return price, data.get("price_unit") or "USD"


async def reconcile_call_price(call_sid: str, estimated: float) -> None:
    """
    Poll Twilio until the call is rated, then overwrite the estimate.

    Retries with a fixed delay rather than exponential backoff: the wait is
    short and bounded, and a predictable schedule is easier to reason about
    when reading logs than a doubling one.

    Never raises. Every failure path leaves the estimate untouched and logged.
    """
    if not config.TWILIO_RECONCILE_PRICE:
        return

    delay = config.TWILIO_RECONCILE_DELAY_SEC
    for attempt in range(1, config.TWILIO_RECONCILE_MAX_ATTEMPTS + 1):
        await asyncio.sleep(delay)

        price, currency = await fetch_call_price(call_sid)
        if price is None:
            logger.debug(
                f"[{call_sid}] Twilio price not rated yet "
                f"(attempt {attempt}/{config.TWILIO_RECONCILE_MAX_ATTEMPTS})"
            )
            continue

        try:
            async with AsyncSessionLocal() as db:
                await crud.update_twilio_cost(
                    db,
                    call_id=call_sid,
                    twilio_cost=price,
                    source="actual",
                    currency=currency,
                )
        except Exception:
            logger.exception(f"[{call_sid}] Could not persist the reconciled Twilio price")
            return

        drift = price - estimated
        # A large gap means the configured rate is wrong, which is exactly the
        # thing this exists to surface. Logged at WARNING so it is visible
        # without anyone having to go looking for it.
        pct = (drift / estimated * 100) if estimated > 0 else 0.0
        level = logger.warning if abs(pct) > 20 else logger.info
        level(
            f"[{call_sid}] Twilio price reconciled: "
            f"estimated ${estimated:.5f} -> actual ${price:.5f} {currency} "
            f"({pct:+.1f}%)"
            + (
                "  ← check TWILIO_*_COST_PER_MINUTE against your invoice"
                if abs(pct) > 20
                else ""
            )
        )
        return

    logger.warning(
        f"[{call_sid}] Twilio price still unrated after "
        f"{config.TWILIO_RECONCILE_MAX_ATTEMPTS} attempts — the estimate stands. "
        f"The row remains marked twilio_cost_source='estimated'."
    )


def dispatch_reconciliation(call_sid: str, estimated: float) -> None:
    """
    Fire-and-forget entry point, called from on_call_end.

    The call is already over, so nothing waits on this. The task reference is
    retained until it finishes — without that, asyncio's weak reference lets it
    be garbage-collected mid-flight.
    """
    if not config.TWILIO_RECONCILE_PRICE:
        return
    task = asyncio.create_task(reconcile_call_price(call_sid, estimated))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain_pending(timeout: float = 30.0) -> None:
    """Let in-flight reconciliations finish during graceful shutdown."""
    if not _pending:
        return
    logger.info(f"Waiting for {len(_pending)} Twilio price reconciliation(s)")
    try:
        await asyncio.wait_for(
            asyncio.gather(*list(_pending), return_exceptions=True), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"{len(_pending)} Twilio reconciliation(s) did not finish before shutdown — "
            f"those rows keep their estimate"
        )