"""
voice/turn_state.py — the smart-hearing / multi-part-merge hold decision
logic (VA-A3 fix), extracted out of voice/stream.py's handle_twilio_stream
so it can be unit-tested without a live WebSocket.

This is a mechanical extraction, not a rewrite: every method mirrors a
closure that used to live inline in handle_twilio_stream, over the same
state, with the same decision math. stream.py still owns all the actual
asyncio task scheduling and websocket sends — this class only decides
"send now, or hold for how long and why", using an injectable clock so
tests can control elapsed time without real sleeps.

Originally had a third responsibility, sustained language-switch
confirmation, extracted at the same time — removed when the rest of the
codebase went English-only (STT is now forced to English at the source;
see voice/stt.py) and voice/stream.py dropped all language-switching.
"""
import dataclasses
import time
from typing import Callable, Optional


@dataclasses.dataclass
class HoldDecision:
    hold: bool
    grace_ms: int = 0
    reason: str = ""


class TurnHoldState:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock

        # Smart hearing (config.ENABLE_SMART_HEARING) — see
        # voice/stream.py's original docstring on pending_fragment for the
        # real-call evidence this guards against.
        self.pending_fragment: Optional[str] = None
        self.pending_started_at: float = 0.0

        # Adaptive multi-part turn merging (config.ENABLE_MULTI_PART_MERGE)
        self.last_speech_started_at: float = 0.0
        self.last_turn_dispatched_at: float = 0.0
        self.multi_part_signal_count: int = 0
        self.multi_part_mode: bool = False

    # ── Fragment combining ──────────────────────────────────────────────

    def combine_with_pending(self, new_text: str) -> str:
        """Merge new_text with whatever is currently held, starting the
        hold-window clock if nothing was pending yet. Does not touch any
        in-flight grace-period task — stream.py is responsible for
        cancelling its own timer when the caller supplies new text."""
        if self.pending_fragment:
            combined = f"{self.pending_fragment} {new_text}".strip()
        else:
            combined = new_text
            self.pending_started_at = self._clock()
        return combined

    def reset_pending(self) -> None:
        self.pending_fragment = None
        self.pending_started_at = 0.0

    # ── Hold-vs-finalize decision ───────────────────────────────────────

    def decide_hold(
        self,
        combined: str,
        *,
        smart_hearing_enabled: bool,
        multi_part_merge_enabled: bool,
        short_fragment_words: int,
        short_fragment_grace_ms: int,
        smart_hearing_grace_ms: int,
        multi_part_hold_grace_ms: int,
        max_hold_ms: int,
        sounds_incomplete_fn: Callable[[str], bool],
    ) -> HoldDecision:
        """Pure decision: should `combined` be held a little longer, or sent
        now? Mirrors voice/stream.py's original _finalize_or_hold exactly —
        see that docstring (still in stream.py) for the three independent
        reasons to hold and why the longest applicable grace window wins.
        Does not mutate state or dispatch anything; the caller (stream.py)
        applies the decision (schedules the grace timer, or clears
        pending state and sends the turn).
        """
        incomplete = smart_hearing_enabled and sounds_incomplete_fn(combined)
        is_short_clause_start = incomplete and len(combined.split()) <= short_fragment_words
        multi_part_candidate = multi_part_merge_enabled and self.multi_part_mode
        held_too_long = (self._clock() - self.pending_started_at) * 1000 >= max_hold_ms

        grace_candidates: list[tuple[int, str]] = []
        if is_short_clause_start:
            grace_candidates.append((short_fragment_grace_ms, "short clause-starter"))
        elif incomplete:
            grace_candidates.append((smart_hearing_grace_ms, "incomplete-sounding"))
        if multi_part_candidate:
            grace_candidates.append((multi_part_hold_grace_ms, "adaptive multi-part pattern"))

        if grace_candidates and not held_too_long:
            grace_ms, _ = max(grace_candidates, key=lambda pair: pair[0])
            reasons = " + ".join(r for _, r in grace_candidates)
            return HoldDecision(hold=True, grace_ms=grace_ms, reason=reasons)

        return HoldDecision(hold=False)

    # ── Adaptive multi-part signal tracking ─────────────────────────────

    def register_multi_part_signal_from_gap(self, trigger_count: int) -> bool:
        """Quick-succession signal: the caller started talking again
        suspiciously soon after the previous turn was dispatched. Returns
        True the instant multi_part_mode newly turns on (so the caller can
        log it), False otherwise — including when it was already on."""
        if self.multi_part_mode:
            return False
        self.multi_part_signal_count += 1
        if self.multi_part_signal_count >= trigger_count:
            self.multi_part_mode = True
            return True
        return False

    def register_multi_part_signal_from_bargein(self, trigger_count: int) -> bool:
        """A real barge-in is itself proof of multi-part intent. Same
        return-value contract as register_multi_part_signal_from_gap."""
        return self.register_multi_part_signal_from_gap(trigger_count)

    def quick_succession_gap_ms(self) -> Optional[float]:
        """Milliseconds between the previous turn being dispatched and the
        caller starting to speak again, or None if either hasn't happened
        yet this call."""
        if self.last_turn_dispatched_at <= 0 or self.last_speech_started_at <= 0:
            return None
        return (self.last_speech_started_at - self.last_turn_dispatched_at) * 1000

    def mark_turn_dispatched(self) -> None:
        self.last_turn_dispatched_at = self._clock()

    def mark_speech_started(self) -> None:
        self.last_speech_started_at = self._clock()
