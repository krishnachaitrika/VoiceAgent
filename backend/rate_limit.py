"""
rate_limit.py — shared slowapi Limiter instance (VA-B3 fix).

Lives in its own module (not main.py) so route modules like
api/twilio_webhook.py can import `limiter` to apply a stricter per-route
limit without importing main.py itself and creating a circular import
(main.py already imports every router module at startup).

VA-T-006 FIX — THE LIMITER WAS COUNTING THE PROXY, NOT THE CALLER
-----------------------------------------------------------------
slowapi's get_remote_address returns the socket peer address. In every
real deployment that peer is an intermediary, not the client:

  - dashboard traffic reaches the backend from the Next.js server
    (frontend/app/api/[...path]/route.js), so EVERY dashboard user shared
    one 60/min bucket and a second person triaging calls tripped 429s
  - /incoming-call arrives from the ingress controller, turning a 30/min
    per-client guard into a GLOBAL ceiling on inbound call volume — at 31
    calls a minute the service would start refusing real customers

Both failure modes are invisible in local testing, where the client really
is the peer.

WHY THIS IS NOT JUST "READ X-FORWARDED-FOR"
-------------------------------------------
That header is client-supplied and trivially forged. Trusting it blindly
lets anyone bypass the limit entirely by sending a different value on each
request — strictly worse than the bug it fixes.

So the header is honoured ONLY when the request actually arrived from an
address we have declared trusted (RATE_LIMIT_TRUSTED_PROXIES). Anything
else falls back to the peer address. With no trusted proxies configured
the behaviour is identical to before: correct for local runs, and a
deployment behind a proxy must name it explicitly rather than inherit a
silent security hole.

We take the RIGHTMOST untrusted entry in the chain rather than the leftmost.
A forged header prepends values, so the left of the chain is attacker
controlled; walking from the right and stopping at the first address our
own infrastructure did not add gives the earliest hop we can actually
vouch for.
"""
import ipaddress
import logging
from typing import List, Optional

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

import config

logger = logging.getLogger(__name__)


def _parse_trusted(raw: str) -> List[ipaddress._BaseNetwork]:
    """Parse RATE_LIMIT_TRUSTED_PROXIES into networks.

    Accepts single addresses ("10.0.0.5") and CIDR blocks ("10.0.0.0/8"),
    because a Kubernetes pod CIDR is the normal way to express "any of our
    own ingress pods" without pinning ephemeral pod IPs.
    """
    networks: List[ipaddress._BaseNetwork] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning(
                f"RATE_LIMIT_TRUSTED_PROXIES: ignoring unparseable entry {entry!r}. "
                f"Use an IP ('10.0.0.5') or a CIDR block ('10.0.0.0/8')."
            )
    return networks


_TRUSTED_PROXIES = _parse_trusted(config.RATE_LIMIT_TRUSTED_PROXIES)


def _is_trusted(addr: Optional[str]) -> bool:
    if not addr or not _TRUSTED_PROXIES:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_PROXIES)


def client_ip(request: Request) -> str:
    """Rate-limit key: the earliest hop we can actually vouch for.

    Falls back to the peer address whenever the forwarded chain cannot be
    trusted, which is both the safe default and identical to the previous
    behaviour for a direct connection.
    """
    peer = get_remote_address(request)

    if not _is_trusted(peer):
        # Either nothing is configured as a proxy, or this request did not
        # come from one — the peer IS the client, and any forwarded header
        # on it is unverifiable.
        return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return peer

    # Right to left: stop at the first hop our own trusted infrastructure
    # did not contribute. Everything left of that may be forged.
    for candidate in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
        if not _is_trusted(candidate):
            return candidate

    # Every hop in the chain is one of ours — the original client address is
    # the leftmost entry.
    first = forwarded.split(",")[0].strip()
    return first or peer


limiter = Limiter(key_func=client_ip, default_limits=[config.RATE_LIMIT_DEFAULT])