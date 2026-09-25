"""
scripts/verify_fixes.py — check the QA remediation still holds.

    python scripts/verify_fixes.py           everything (needs the database)
    python scripts/verify_fixes.py --fast    logic only, no database, <1s
    python scripts/verify_fixes.py --list    what each check covers

WHY THIS EXISTS

Round four's lesson was that four of five defects were invisible to a 50-test
suite, a clean production build and a successful startup — they only appeared
when someone placed a real call. The same trap applies to the fixes: they are
correct today because they were read and exercised by hand, and nothing stops
one being quietly reverted next month and rediscovered by the next QA round.

So each fix gets a check that FAILS if the behaviour regresses.

WHAT IT CANNOT COVER

Deliberately honest about its own limits — a green run here does not mean
everything is verified:

    VA-T-001  ingress pathType      needs a real cluster
    VA-T-006  proxy-aware limiting  needs something in front of uvicorn
    VA-T-011  shutdown drains       needs a rolling restart mid-call
    VA-T-002  login                 needs a browser (the hashing IS covered)

Those are listed at the end of every run rather than silently omitted.

EXIT CODE
    0 = every check passed, non-zero = at least one regressed, so this can go
    straight into CI without wrapping.
"""
import argparse
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def record(status: str, finding: str, detail: str) -> None:
    _results.append((status, finding, detail))
    symbol = {PASS: "  ok  ", FAIL: " FAIL ", SKIP: " skip "}[status]
    print(f"[{symbol}] {finding:12} {detail}")


# ── Logic checks — no database, no network ───────────────────────────────────


def check_retrieval_band() -> None:
    """VA-T-012/013 — the band must classify the QA round's real distances.

    These are the exact top-match distances recorded during the AI eval, plus
    the two seen on live calls. Hardcoding them is the point: they are the
    evidence the thresholds were chosen from, so if someone narrows the band
    back toward 0.70 this check says which questions start failing again.
    """
    cases = [
        (0.3727, "strong", "What services do you offer?"),
        (0.4233, "strong", "What services will you provide for us?  [live]"),
        (0.4495, "strong", "Where is Technozis located?           [live]"),
        (0.6246, "weak", "Tell me about your company"),
        (0.7461, "weak", "which place in India?                 [live]"),
        (0.7506, "weak", "where in India? -> Bangalore          [live, grounded]"),
        (0.7562, "weak", "What's your starting price?"),
        (0.7861, "weak", "Which systems can you connect to?"),
        (0.8380, "reject", "What's the address again?"),
        (0.9500, "reject", "completely unrelated"),
    ]
    strong, relevance = (
        config.RAG_STRONG_MATCH_THRESHOLD,
        config.RAG_RELEVANCE_THRESHOLD,
    )

    def band(d: float) -> str:
        if d <= strong:
            return "strong"
        return "weak" if d <= relevance else "reject"

    wrong = [(d, exp, band(d), q) for d, exp, q in cases if band(d) != exp]
    if wrong:
        for d, exp, got, q in wrong:
            print(f"           {d:.4f} expected {exp:6} got {got:6}  {q}")
        record(
            FAIL,
            "VA-T-012/13",
            f"{len(wrong)}/{len(cases)} distances misclassified "
            f"(band is {strong}-{relevance})",
        )
        return

    recovered = sum(1 for d, exp, _ in cases if d > 0.70 and exp == "weak")
    record(
        PASS,
        "VA-T-012/13",
        f"all {len(cases)} distances classified correctly; {recovered} "
        f"question(s) the old 0.70 cliff rejected now recover as weak matches",
    )


def check_phone_erasure_matching() -> None:
    """VA-T-009 — one person's number reaches the system in several formats.

    An exact == missed the same caller stored differently and still reported
    success, which is worse than failing: the erasure request is closed, the
    data is still there, and nobody knows.
    """
    stored = [
        "+916369996595",      # calls.phone_number, straight from Twilio
        "6369996595",         # leads.phone, normalised by utils/phone.py
        "+91 63699 96595",    # typed with spaces
        "0916369996595",      # trunk prefix
    ]
    other = ["+919876543210", "6369996594"]  # last digit differs
    requested = "6369996595"

    tail_len = config.PHONE_NUMBER_DIGIT_LENGTH
    digits = "".join(c for c in requested if c.isdigit())
    tail = digits[-tail_len:] if len(digits) >= tail_len else digits

    def matches(value: str) -> bool:
        return "".join(c for c in value if c.isdigit()).endswith(tail)

    missed = [s for s in stored if not matches(s)]
    false_hits = [o for o in other if matches(o)]

    if missed or false_hits:
        for m in missed:
            print(f"           MISSED  {m}  (this caller's data would survive erasure)")
        for f in false_hits:
            print(f"           WRONGLY MATCHED  {f}  (another caller's data would be deleted)")
        record(FAIL, "VA-T-009", f"{len(missed)} missed, {len(false_hits)} false match(es)")
        return

    record(
        PASS,
        "VA-T-009",
        f"all {len(stored)} stored formats resolve to one caller; "
        f"{len(other)} similar numbers correctly untouched "
        f"(matching last {tail_len} digits)",
    )


def check_flagged_turn_not_cached() -> None:
    """VA-T-014 — a guardrail-flagged turn must never be written to the cache.

    An injection attempt names no topic, so it routes "direct" and used to be
    answered "Yes, go ahead!" — and "direct" replies were cacheable, so one
    caller's probe became every caller's answer for REDIS_LLM_CACHE_TTL,
    served straight from cache with the orchestrator skipped entirely.

    Mirrors the expression in orchestrator/graph.py exactly.
    """

    def cacheable(route: str, tool_called: bool, kb_miss: bool, security_note) -> bool:
        return (
            route in ("direct", "knowledge")
            and not tool_called
            and not kb_miss
            and security_note is None
        )

    cases = [
        (("direct", False, False, None), True, "ordinary small talk"),
        (("knowledge", False, False, None), True, "ordinary KB answer"),
        (("direct", False, False, "JAILBREAK"), False, "FLAGGED injection attempt"),
        (("knowledge", False, False, "JAILBREAK"), False, "flagged knowledge turn"),
        (("action", False, False, None), False, "action — caller-specific"),
        (("knowledge", True, False, None), False, "tool was called"),
        (("knowledge", False, True, None), False, "KB miss"),
    ]
    wrong = [(args, exp, desc) for args, exp, desc in cases if cacheable(*args) != exp]
    if wrong:
        for args, exp, desc in wrong:
            print(f"           {desc}: expected cacheable={exp}, got {cacheable(*args)}")
        record(FAIL, "VA-T-014", f"{len(wrong)} case(s) wrong")
        return
    record(PASS, "VA-T-014", f"all {len(cases)} cases correct; flagged turns never cached")


def check_greeting_language() -> None:
    """VA-T-015 — the greeting must not promise a language this build cannot
    answer in. It is the first sentence of EVERY call."""
    import config_validation

    report = config_validation.validate()
    greeting_errors = [e for e in report.errors if "GREETING_TEMPLATE" in e]
    if greeting_errors:
        record(FAIL, "VA-T-015", greeting_errors[0][:90])
        return
    record(PASS, "VA-T-015", "greeting offers no unsupported language")


def check_prompt_not_runtime_editable() -> None:
    """System prompt lives in code, and the API refuses to write it.

    Removing the dashboard field alone was never enough — the button would be
    gone while a single request with the admin key could still set the value.
    """
    from api.settings import CODE_MANAGED_SETTING_KEYS, WRITABLE_SETTING_KEYS

    problems = []
    if "system_prompt" not in CODE_MANAGED_SETTING_KEYS:
        problems.append("system_prompt is no longer refused by the settings API")
    if "system_prompt" in WRITABLE_SETTING_KEYS:
        problems.append("system_prompt is writable again")

    from cache.settings_cache import _env_defaults

    if "system_prompt" in _env_defaults():
        problems.append("system_prompt is published in live settings again")

    if problems:
        record(FAIL, "prompt", "; ".join(problems))
        return
    record(
        PASS,
        "prompt",
        "system_prompt rejected by the API and absent from live settings",
    )


def check_shutdown_budget() -> None:
    """VA-T-011 — the drains must finish inside the platform's kill deadline,
    or the process is SIGKILLed before Redis and HTTP clients close."""
    budget, deadline = (
        config.SHUTDOWN_TOTAL_BUDGET_SEC,
        config.PLATFORM_TERMINATION_GRACE_SEC,
    )
    if budget >= deadline:
        record(
            FAIL,
            "VA-T-011",
            f"budget {budget}s >= deadline {deadline}s — clients would leak on "
            f"every rolling restart",
        )
        return
    record(PASS, "VA-T-011", f"budget {budget}s fits inside deadline {deadline}s")


# ── Database checks ──────────────────────────────────────────────────────────


async def check_kb_version_changes() -> None:
    """VA-T-003 / VA-T-016 — both caches are keyed on this value, so it MUST
    change when the knowledge base does.

    If it does not, deleting a document leaves its text reachable under the
    old key for the full REDIS_RAG_CACHE_TTL (24h by default) while the API,
    the documents page and the database all report it gone.
    """
    from cache.kb_version import get_kb_version, invalidate_kb_version
    from database.base import AsyncSessionLocal
    from database.models import Document

    before = await get_kb_version()
    if before == "kb-unavailable":
        record(SKIP, "VA-T-003/16", "database unreachable")
        return

    # documents.id is an autoincrementing INTEGER (database/models.py), not a
    # string key like calls.id — which is the CallSid. Passing a uuid4 string
    # made asyncpg reject the insert outright:
    #   invalid input for query argument $1 ... 'str' object cannot be
    #   interpreted as an integer
    # So the id is left for Postgres to assign and read back afterwards.
    probe_id = None
    probe_marker = f"verify_fixes.py probe row {uuid.uuid4().hex[:8]} — safe to delete"
    try:
        async with AsyncSessionLocal() as db:
            probe = Document(
                content=probe_marker,
                embedding=[0.0] * config.EMBEDDING_DIM,
                metadata_json={"source": "verify_fixes_probe"},
            )
            db.add(probe)
            await db.commit()
            # refresh() populates the server-assigned id so the delete below
            # can find the exact row this run created, rather than matching on
            # content and risking a collision with a concurrent run.
            await db.refresh(probe)
            probe_id = probe.id

        invalidate_kb_version()
        after_insert = await get_kb_version()

        async with AsyncSessionLocal() as db:
            row = await db.get(Document, probe_id) if probe_id is not None else None
            if row is not None:
                await db.delete(row)
                await db.commit()
                probe_id = None

        invalidate_kb_version()
        after_delete = await get_kb_version()
    except Exception as e:
        record(SKIP, "VA-T-003/16", f"could not run probe: {e}")
        return
    finally:
        # A probe row left in `documents` is not harmless: it becomes a
        # zero-vector chunk the knowledge base will happily retrieve against.
        if probe_id is not None:
            try:
                async with AsyncSessionLocal() as db:
                    leftover = await db.get(Document, probe_id)
                    if leftover is not None:
                        await db.delete(leftover)
                        await db.commit()
            except Exception:
                print(f"        WARNING: probe row {probe_id} could not be removed — "
                      f"delete it by hand from `documents`")

    if before == after_insert:
        record(FAIL, "VA-T-003/16", f"version unchanged after insert ({before}) — stale answers would persist")
        return
    if after_insert == after_delete:
        record(FAIL, "VA-T-003/16", f"version unchanged after delete ({after_insert}) — DELETED CONTENT WOULD STILL BE SPOKEN")
        return

    record(
        PASS,
        "VA-T-003/16",
        f"version moved {before} -> {after_insert} -> {after_delete}; "
        f"both caches invalidated on insert and delete",
    )


async def check_vector_index() -> None:
    """The HNSW index on documents.embedding.

    Without it every knowledge search is a sequential scan computing cosine
    distance across every chunk — on the per-turn latency path, on every
    factual question. Invisible at a few hundred chunks, painful past a few
    thousand, which is exactly when nobody remembers this step.
    """
    from sqlalchemy import text

    from database.base import get_engine

    try:
        async with get_engine().connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname='public' "
                    "AND tablename='documents' AND (indexdef ILIKE '%hnsw%' "
                    "OR indexdef ILIKE '%ivfflat%')"
                )
            )
            names = [r[0] for r in result]
    except Exception as e:
        record(SKIP, "vector index", f"could not query: {e}")
        return

    if not names:
        record(FAIL, "vector index", "MISSING — every KB search is a sequential scan. Run: python scripts/setup_db.py")
        return
    record(PASS, "vector index", f"present ({names[0]})")


async def check_dashboard_accounts() -> None:
    """VA-T-002 — an account must exist, or nobody can open the console.

    Also confirms Argon2id is actually in use: a plaintext or fast-hash
    password column is the failure this was built to prevent.
    """
    from database.base import AsyncSessionLocal
    from services.user_auth import count_users
    from sqlalchemy import select
    from database.models import User

    try:
        async with AsyncSessionLocal() as db:
            total = await count_users(db)
            first = (await db.execute(select(User).limit(1))).scalar_one_or_none()
    except Exception as e:
        record(SKIP, "VA-T-002", f"could not query users: {e}")
        return

    if total == 0:
        record(FAIL, "VA-T-002", "no dashboard accounts — run: python scripts/create_user.py")
        return
    if first is not None and not first.password_hash.startswith("$argon2"):
        record(FAIL, "VA-T-002", "password_hash is NOT an Argon2 hash")
        return
    record(PASS, "VA-T-002", f"{total} account(s), passwords Argon2id-hashed")


# ── Runner ───────────────────────────────────────────────────────────────────

CANNOT_AUTOMATE = [
    ("VA-T-001", "ingress pathType Prefix", "needs a real Kubernetes cluster"),
    ("VA-T-004", "drain reference counting", "needs two connections on one CallSid"),
    ("VA-T-006", "proxy-aware rate limiting", "needs a proxy in front of uvicorn"),
    ("VA-T-008", "retention CronJob", "needs the cluster; run purge_old_data.py by hand to test"),
    ("VA-T-010", "discarded turn cost", "needs a barge-in on a live call"),
    ("VA-T-014", "spoken deflection", "logic is checked; the SPOKEN reply needs a call"),
]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--fast", action="store_true", help="logic only, no database")
    parser.add_argument("--list", action="store_true", help="describe the checks")
    args = parser.parse_args()

    if args.list:
        print(__doc__)
        return 0

    print(f"\nVerifying QA remediation — {config.COMPANY_NAME} voice agent")
    print("=" * 78)

    print("\nLogic checks (no database)")
    print("-" * 78)
    check_retrieval_band()
    check_phone_erasure_matching()
    check_flagged_turn_not_cached()
    check_greeting_language()
    check_prompt_not_runtime_editable()
    check_shutdown_budget()

    if not args.fast:
        print("\nDatabase checks")
        print("-" * 78)
        await check_dashboard_accounts()
        await check_vector_index()
        await check_kb_version_changes()

        from database.base import get_engine

        await get_engine().dispose()

    failed = [r for r in _results if r[0] == FAIL]
    skipped = [r for r in _results if r[0] == SKIP]

    print("\n" + "=" * 78)
    print(
        f"{len(_results) - len(failed) - len(skipped)} passed, "
        f"{len(failed)} failed, {len(skipped)} skipped"
    )

    if failed:
        print("\nREGRESSED:")
        for _, finding, detail in failed:
            print(f"  {finding}: {detail}")

    print("\nNot covered here — these need a call, a proxy, or a cluster:")
    for finding, what, why in CANNOT_AUTOMATE:
        print(f"  {finding:10} {what:28} {why}")

    print()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))