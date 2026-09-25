"""
Unit tests for voice/turn_state.py (VA-A3 extraction). Uses a fake,
manually-advanced clock instead of real sleeps, so grace-window/hold-ceiling
behaviour is deterministic and instant to test.
"""
import pytest

from voice.turn_state import TurnHoldState


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_state():
    clock = FakeClock()
    return TurnHoldState(clock=clock), clock


class TestCombineWithPending:
    def test_first_fragment_starts_the_hold_clock(self):
        state, clock = make_state()
        clock.advance(5.0)
        combined = state.combine_with_pending("Could you")
        assert combined == "Could you"
        assert state.pending_started_at == 5.0

    def test_merges_with_existing_pending_fragment(self):
        state, clock = make_state()
        state.pending_fragment = "Could you"
        combined = state.combine_with_pending("explain the pricing?")
        assert combined == "Could you explain the pricing?"

    def test_reset_pending_clears_state(self):
        state, clock = make_state()
        state.combine_with_pending("Could you")
        state.reset_pending()
        assert state.pending_fragment is None
        assert state.pending_started_at == 0.0


class TestDecideHold:
    def _decide(self, state, combined, **overrides):
        params = dict(
            smart_hearing_enabled=True,
            multi_part_merge_enabled=True,
            short_fragment_words=3,
            short_fragment_grace_ms=1600,
            smart_hearing_grace_ms=700,
            multi_part_hold_grace_ms=900,
            max_hold_ms=3000,
            sounds_incomplete_fn=lambda t: t.endswith("and"),
        )
        params.update(overrides)
        return state.decide_hold(combined, **params)

    def test_complete_sentence_is_not_held(self):
        state, clock = make_state()
        state.combine_with_pending("What are your working hours?")
        decision = self._decide(state, "What are your working hours?")
        assert decision.hold is False

    def test_incomplete_sentence_is_held_with_grace(self):
        state, clock = make_state()
        state.combine_with_pending("I wanted to ask about pricing and")
        decision = self._decide(state, "I wanted to ask about pricing and")
        assert decision.hold is True
        assert decision.grace_ms == 700
        assert "incomplete" in decision.reason

    def test_short_clause_starter_gets_longer_grace(self):
        state, clock = make_state()
        state.combine_with_pending("Could you")
        decision = self._decide(
            state, "Could you", sounds_incomplete_fn=lambda t: True
        )
        assert decision.hold is True
        assert decision.grace_ms == 1600
        assert "short clause-starter" in decision.reason

    def test_multi_part_mode_holds_even_complete_sentences(self):
        state, clock = make_state()
        state.multi_part_mode = True
        state.combine_with_pending("What are your working hours?")
        decision = self._decide(state, "What are your working hours?")
        assert decision.hold is True
        assert decision.grace_ms == 900
        assert "adaptive multi-part pattern" in decision.reason

    def test_longest_grace_window_wins_when_multiple_reasons_apply(self):
        state, clock = make_state()
        state.multi_part_mode = True
        state.combine_with_pending("Could you")
        decision = self._decide(
            state, "Could you", sounds_incomplete_fn=lambda t: True
        )
        # short-clause-starter (1600ms) beats multi-part (900ms).
        assert decision.grace_ms == 1600

    def test_held_too_long_forces_send_even_if_incomplete(self):
        state, clock = make_state()
        state.combine_with_pending("I wanted to ask about pricing and")
        clock.advance(3.5)  # exceeds max_hold_ms=3000
        decision = self._decide(state, "I wanted to ask about pricing and")
        assert decision.hold is False


class TestMultiPartSignals:
    def test_becomes_sticky_after_trigger_count_reached(self):
        state, clock = make_state()
        first = state.register_multi_part_signal_from_gap(trigger_count=2)
        assert first is False  # count=1, not yet triggered
        assert state.multi_part_mode is False
        second = state.register_multi_part_signal_from_gap(trigger_count=2)
        assert second is True  # count=2, just turned on
        assert state.multi_part_mode is True

    def test_no_further_signal_once_already_on(self):
        state, clock = make_state()
        state.multi_part_mode = True
        assert state.register_multi_part_signal_from_bargein(trigger_count=1) is False

    def test_quick_succession_gap_ms(self):
        state, clock = make_state()
        assert state.quick_succession_gap_ms() is None  # neither event happened yet
        clock.advance(1.0)
        state.mark_turn_dispatched()
        clock.advance(0.3)
        state.mark_speech_started()
        assert state.quick_succession_gap_ms() == pytest.approx(300.0)
