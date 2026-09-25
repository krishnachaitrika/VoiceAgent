"""
scripts/verify_round8.py — prove the round-eight additions work AND that they
cannot affect a deployment that has not configured them.

    python scripts/verify_round8.py                 checks only, sends nothing
    python scripts/verify_round8.py --send-test     actually delivers a test alert

WHY THE SECOND FLAG IS SEPARATE

Sending a real Slack message or SMS is not something a verification script
should do by surprise — someone running this at 2am should not page the on-call
engineer. So delivery is opt-in and clearly labelled in the message itself.

WHAT THIS CHECKS THAT MATTERS MOST

The headline additions (escalation alerts, error tracking) are useful. But the
requirement was that the agent already works and must not be disturbed, so the
FIRST section verifies the no-op property: with nothing configured, dispatch
returns in microseconds, creates no task, and the Sentry helpers do nothing.

That is the property that protects the working product, and it is the one most
likely to be broken by a careless future change.
"""
import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

PASS, FAIL, INFO = "[  ok  ]", "[ FAIL ]", "[ info ]"
results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"{PASS if ok else FAIL} {name:<28} {detail}")


# ─── 1. The no-op guarantee ────────────────────────────────────────────────


async def check_no_op() -> None:
    print("\nNo-op guarantee — an unconfigured deployment must be unaffected")
    print("-" * 78)
    from notifications.notifier import (
        EscalationAlert,
        configured_channels,
        dispatch_escalation,
    )
    from observability.error_tracking import capture_exception, is_enabled, set_call_context

    channels = configured_channels()

    if channels:
        record(True, "channels configured", f"{', '.join(channels)} — no-op test skipped")
    else:
        tasks_before = len(asyncio.all_tasks())
        started = time.perf_counter()
        dispatch_escalation(
            EscalationAlert(call_id="noop-check", reason="verification", phone_number="+910000000000")
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        tasks_after = len(asyncio.all_tasks())

        record(
            tasks_after == tasks_before,
            "dispatch creates no task",
            f"{tasks_after - tasks_before} created (must be 0)",
        )
        # Generous bound: the point is "returns immediately", not a benchmark.
        record(elapsed_ms < 1.0, "dispatch returns instantly", f"{elapsed_ms:.4f} ms")

    # These must never raise, enabled or not — they sit on the error path,
    # where a second failure is hardest to diagnose.
    try:
        set_call_context("noop-check")
        capture_exception(ValueError("verification probe"), source="verify_round8")
        record(True, "error-tracking helpers safe", f"enabled={is_enabled()}")
    except Exception as e:
        record(False, "error-tracking helpers safe", f"raised {e!r}")


# ─── 2. PII scrubbing ──────────────────────────────────────────────────────


def check_pii_scrubbing() -> None:
    print("\nPII redaction — caller numbers must not reach a third party")
    print("-" * 78)
    from observability.error_tracking import _scrub

    cases = [
        ("Call from +916369996595 failed", True),
        ("lead phone 6369996595 rejected", True),
        ("+91 63699 96595 could not be reached", True),
        ("TimeoutError after 30s", False),
    ]
    all_ok = True
    for text, should_redact in cases:
        scrubbed = _scrub(text)
        redacted = "[phone-redacted]" in scrubbed
        ok = redacted == should_redact
        all_ok &= ok
        print(f"        {'redacted ' if redacted else 'untouched'}  {text[:46]}")
    record(all_ok, "phone numbers redacted", "and non-phone text left intact")


# ─── 3. Payload shape ──────────────────────────────────────────────────────


def check_payloads() -> None:
    print("\nAlert payloads — masked, bounded, correctly shaped")
    print("-" * 78)
    from notifications.notifier import EscalationAlert

    alert = EscalationAlert(
        call_id="CAverify",
        reason="Caller became frustrated and asked for a human",
        phone_number="+916369996595",
        transcript_snippet="x" * (config.ESCALATION_SNIPPET_MAX_CHARS + 500),
    )

    slack = alert.as_slack_payload()
    record(
        "text" in slack and "blocks" in slack,
        "slack payload shape",
        "has both text (push preview) and blocks",
    )

    body = alert.as_sms_body()
    record(len(body) <= 300, "sms body bounded", f"{len(body)} chars")

    webhook = alert.as_webhook_payload()
    full_number_leaked = "6369996595" in str(webhook)
    record(not full_number_leaked, "caller number masked", f"sent as {webhook['caller']}")

    snippet_len = len(webhook["transcript_snippet"])
    record(
        snippet_len <= config.ESCALATION_SNIPPET_MAX_CHARS + 1,
        "transcript excerpt truncated",
        f"{snippet_len} chars (limit {config.ESCALATION_SNIPPET_MAX_CHARS})",
    )


# ─── 4. VA-T-005 arithmetic ────────────────────────────────────────────────


def check_db_ceiling() -> None:
    print("\nVA-T-005 — database connections at full scale")
    print("-" * 78)
    per_pod = config.MAX_CONCURRENT_CALLS * 2
    worst = per_pod * config.MAX_REPLICAS
    print(f"        {config.MAX_CONCURRENT_CALLS} calls x 2 (pool+overflow) = {per_pod} per pod")
    print(f"        x {config.MAX_REPLICAS} replicas = {worst} connections")
    print(f"        ceiling: {config.DB_CONNECTION_CEILING}")

    within = worst <= config.DB_CONNECTION_CEILING
    record(
        True,  # informational: this reports, it does not fail the run
        "connection ceiling",
        f"{worst}/{config.DB_CONNECTION_CEILING} — "
        + ("within limit" if within else "OVER LIMIT, startup will warn"),
    )
    if not within:
        print("        Options: PgBouncer in front, lower MAX_CONCURRENT_CALLS or")
        print("        MAX_REPLICAS, or one database per client.")


# ─── 5. Optional live delivery ─────────────────────────────────────────────


async def send_test_alert() -> None:
    print("\nLive delivery test")
    print("-" * 78)
    from notifications.notifier import EscalationAlert, close_notifier, configured_channels, notify_escalation

    channels = configured_channels()
    if not channels:
        print(f"{INFO} nothing configured — set ESCALATION_SLACK_WEBHOOK_URL,")
        print("        ESCALATION_SMS_TO or ESCALATION_WEBHOOK_URL first.")
        return

    print(f"        sending to: {', '.join(channels)}")
    await notify_escalation(
        EscalationAlert(
            call_id="VERIFY-TEST",
            reason="TEST ALERT from verify_round8.py — no action needed",
            phone_number="+910000000000",
            transcript_snippet="This is a verification message, not a real escalation.",
        )
    )
    await close_notifier()
    print(f"{INFO} sent — check the channel(s) above")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--send-test",
        action="store_true",
        help="actually deliver a test alert to every configured channel",
    )
    args = parser.parse_args()

    print("=" * 78)
    print(" Round eight — escalation alerts, error tracking, connection ceiling")
    print("=" * 78)

    await check_no_op()
    check_pii_scrubbing()
    check_payloads()
    check_db_ceiling()

    if args.send_test:
        await send_test_alert()

    passed = sum(1 for ok, _, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 78)
    print(f" {passed}/{total} checks passed")
    print("=" * 78)

    if passed != total:
        sys.exit(1)

    print("\nStill needs a real call or a cluster:")
    print("  escalation alert   trigger an escalation on a live call")
    print("  error tracking     set SENTRY_DSN, then force an error")
    print("  VA-T-005           only observable at full HPA scale")


if __name__ == "__main__":
    asyncio.run(main())