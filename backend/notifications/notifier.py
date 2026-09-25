"""
notifications/notifier.py — tell a human when a call escalates.

THE GAP THIS CLOSES

`_tool_escalate` wrote an `escalations` row, flipped the call status, published
to the event bus — and told nobody. Then it said this to the caller:

    "Escalated successfully. Our team will follow up with you shortly."

A promise the system had no mechanism to keep. An angry caller at 2am heard it
and their row sat in a dashboard nobody was watching until morning.

DESIGN CONSTRAINT: THIS MUST NOT CHANGE HOW A CALL BEHAVES

The agent already works. Nothing here may slow a turn, block a reply, or
create a new way for a call to fail. So:

  - NO-OP WHEN UNCONFIGURED. With no channel set, dispatch_escalation returns
    immediately. An existing deployment that pulls this code and changes
    nothing behaves exactly as it does today.

  - NEVER ON THE CALL PATH. Delivery runs in a task the caller's turn does not
    await. If Slack is down or slow, the caller still gets their reply at the
    same speed.

  - CANNOT RAISE INTO THE CALL. Every failure is caught and logged. The
    database row is the source of truth; this is best-effort delivery on top
    of it, and a failed notification must never turn a working escalation into
    a broken call.

  - BOUNDED. Every request has an explicit timeout, so a hanging webhook
    cannot pin a task indefinitely.

  - httpx, NOT the Twilio SDK. The SDK's client is synchronous and would block
    the event loop — the same class of bug as the Google Calendar call that
    froze audio for every concurrent caller.

PII

The phone number is masked and the transcript excerpt truncated. A Slack
channel usually has a wider audience and a longer retention period than your
database; pasting a full transcript there is a data-protection problem waiting
to happen. The dashboard link carries the detail instead.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

import httpx

import config
from utils.privacy import mask_phone

logger = logging.getLogger(__name__)

# One shared client. A client per notification would add a TLS handshake to
# every escalation and leak sockets under load.
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(config.NOTIFICATION_TIMEOUT_SEC, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=10),
        )
    return _client


async def close_notifier() -> None:
    """Called from main.py's lifespan shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def configured_channels() -> List[str]:
    """Which channels are actually set up.

    Reported at startup so "nobody is being alerted" is visible before an
    escalation depends on it, rather than discovered afterwards.
    """
    names = []
    if config.ESCALATION_SLACK_WEBHOOK_URL:
        names.append("slack")
    if config.ESCALATION_SMS_TO:
        names.append("sms")
    if config.ESCALATION_WEBHOOK_URL:
        names.append("webhook")
    return names


@dataclass
class EscalationAlert:
    call_id: str
    reason: str
    phone_number: str = "unknown"
    transcript_snippet: str = ""

    def dashboard_url(self) -> Optional[str]:
        base = config.DASHBOARD_BASE_URL
        return f"{base}/escalations" if base else None

    def _snippet(self) -> str:
        limit = config.ESCALATION_SNIPPET_MAX_CHARS
        s = (self.transcript_snippet or "").strip()
        return s if len(s) <= limit else s[:limit].rstrip() + "…"

    def as_slack_payload(self) -> dict:
        blocks: List[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Escalation — {config.COMPANY_NAME}"},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Reason:* {self.reason}"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Caller*\n{mask_phone(self.phone_number)}"},
                    {"type": "mrkdwn", "text": f"*Call ID*\n`{self.call_id}`"},
                ],
            },
        ]
        snippet = self._snippet()
        if snippet:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{snippet}```"}}
            )
        url = self.dashboard_url()
        if url:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Open dashboard"},
                            "url": url,
                        }
                    ],
                }
            )
        # `text` is the push-notification preview and the fallback for clients
        # that cannot render blocks — without it Slack shows an empty alert.
        return {"text": f"Escalation: {self.reason}", "blocks": blocks}

    def as_sms_body(self) -> str:
        # Readable on a lock screen. An SMS that needs scrolling defeats the
        # point of paging someone; detail lives in the dashboard.
        body = (
            f"{config.COMPANY_NAME} voice agent escalation\n"
            f"Caller: {mask_phone(self.phone_number)}\n"
            f"Reason: {self.reason[:80]}"
        )
        return body[:300]

    def as_webhook_payload(self) -> dict:
        return {
            "event": "escalation_created",
            "company": config.COMPANY_NAME,
            "agent": config.AGENT_NAME,
            "call_id": self.call_id,
            "caller": mask_phone(self.phone_number),
            "reason": self.reason,
            "transcript_snippet": self._snippet(),
            "dashboard_url": self.dashboard_url(),
        }


# ─── Channels ────────────────────────────────────────────────────────────────


async def _send_slack(alert: EscalationAlert) -> bool:
    if not config.ESCALATION_SLACK_WEBHOOK_URL:
        return False
    resp = await _get_client().post(
        config.ESCALATION_SLACK_WEBHOOK_URL, json=alert.as_slack_payload()
    )
    resp.raise_for_status()
    return True


async def _send_sms(alert: EscalationAlert) -> bool:
    if not (
        config.ESCALATION_SMS_TO
        and config.TWILIO_ACCOUNT_SID
        and config.TWILIO_AUTH_TOKEN
        and config.TWILIO_PHONE_NUMBER
    ):
        return False
    resp = await _get_client().post(
        f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json",
        auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
        data={
            "To": config.ESCALATION_SMS_TO,
            "From": config.TWILIO_PHONE_NUMBER,
            "Body": alert.as_sms_body(),
        },
    )
    resp.raise_for_status()
    return True


async def _send_webhook(alert: EscalationAlert) -> bool:
    if not config.ESCALATION_WEBHOOK_URL:
        return False
    headers = {"Content-Type": "application/json"}
    if config.ESCALATION_WEBHOOK_TOKEN:
        headers["Authorization"] = f"Bearer {config.ESCALATION_WEBHOOK_TOKEN}"
    resp = await _get_client().post(
        config.ESCALATION_WEBHOOK_URL, json=alert.as_webhook_payload(), headers=headers
    )
    resp.raise_for_status()
    return True


_CHANNELS = (("slack", _send_slack), ("sms", _send_sms), ("webhook", _send_webhook))


async def notify_escalation(alert: EscalationAlert) -> None:
    """Fire every configured channel concurrently. Never raises."""
    if not config.ENABLE_ESCALATION_NOTIFICATIONS:
        return

    results = await asyncio.gather(
        *(fn(alert) for _, fn in _CHANNELS), return_exceptions=True
    )

    sent, failed = [], []
    for (name, _), result in zip(_CHANNELS, results):
        if isinstance(result, Exception):
            failed.append(name)
            logger.error(f"[{alert.call_id}] Escalation notify via {name} failed: {result}")
        elif result:
            sent.append(name)

    if sent:
        logger.info(f"[{alert.call_id}] Escalation notified via: {', '.join(sent)}")
    elif failed:
        logger.error(
            f"[{alert.call_id}] Escalation recorded but EVERY channel failed "
            f"({', '.join(failed)}) — nobody has been alerted."
        )
    # No channel configured at all is reported once at startup rather than on
    # every escalation; repeating it per call would be noise in the one log
    # someone is reading during an incident.


# Strong references to in-flight notifications. asyncio holds only a WEAK
# reference to a bare create_task, so a task nothing keeps a handle on can be
# garbage-collected before it runs — the same bug already fixed twice in this
# codebase (analytics.analyze_call, billing.twilio_reconcile).
_pending: set = set()


def dispatch_escalation(alert: EscalationAlert) -> None:
    """Non-blocking entry point. Safe to call from the call path.

    Returns immediately when nothing is configured, so a deployment that has
    not set a channel behaves exactly as it did before this module existed.
    """
    if not config.ENABLE_ESCALATION_NOTIFICATIONS or not configured_channels():
        return
    task = asyncio.create_task(notify_escalation(alert))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain_pending(timeout: float = 10.0) -> None:
    """Let in-flight notifications finish during graceful shutdown, so a deploy
    does not swallow an alert that was seconds from delivery."""
    if not _pending:
        return
    logger.info(f"Waiting for {len(_pending)} escalation notification(s)")
    try:
        await asyncio.wait_for(
            asyncio.gather(*list(_pending), return_exceptions=True), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"{len(_pending)} escalation notification(s) did not finish before shutdown"
        )