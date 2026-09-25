"""Unit tests for voice/stream.py's already-standalone text helpers."""
from voice.stream import (
    is_filler_transcript,
    split_into_sentences,
    sounds_incomplete,
    deduplicate_transcript,
    strip_leading_greeting,
)


class TestIsFillerTranscript:
    def test_drops_true_filler_words(self):
        assert is_filler_transcript("um") is True
        assert is_filler_transcript("hmm") is True
        assert is_filler_transcript("uh mm") is True

    def test_keeps_short_meaningful_answers(self):
        # Real-bug fix: "yes"/"no"/"okay" must never be dropped as filler.
        assert is_filler_transcript("yes") is False
        assert is_filler_transcript("no") is False
        assert is_filler_transcript("okay") is False

    def test_keeps_real_short_phrase(self):
        assert is_filler_transcript("Book a meeting") is False

    def test_drops_very_short_noise(self):
        # Under 3 characters after stripping punctuation — treated as noise.
        assert is_filler_transcript("hi") is True
        assert is_filler_transcript("..") is True


class TestSplitIntoSentences:
    def test_splits_on_terminal_punctuation(self):
        result = split_into_sentences("Hello there. How can I help you?")
        assert result == ["Hello there.", "How can I help you?"]

    def test_splits_long_sentence_on_commas(self):
        long_sentence = (
            "We offer several services, including consulting, development, "
            "and support, all delivered by our expert team of engineers."
        )
        result = split_into_sentences(long_sentence)
        assert len(result) > 1
        assert all(len(s.strip()) > 2 for s in result)

    def test_drops_trivial_fragments(self):
        result = split_into_sentences("Ok. Sure.")
        assert "" not in result


class TestSoundsIncomplete:
    def test_trailing_conjunction_is_incomplete(self):
        assert sounds_incomplete("I wanted to ask about the pricing and") is True

    def test_terminal_punctuation_is_complete(self):
        assert sounds_incomplete("What are your working hours?") is False

    def test_short_no_punctuation_is_incomplete(self):
        assert sounds_incomplete("Could you") is True

    def test_empty_is_not_incomplete(self):
        assert sounds_incomplete("") is False


class TestDeduplicateTranscript:
    def test_removes_repeated_sentences(self):
        result = deduplicate_transcript("I need help. I need help.")
        assert result == "I need help."

    def test_removes_consecutive_repeated_words(self):
        result = deduplicate_transcript("the the quick quick fox")
        assert result == "the quick fox"


class TestStripLeadingGreeting:
    def test_strips_greeting_word_anywhere(self):
        result = strip_leading_greeting("Okay, hello. Can you explain the service?")
        assert "hello" not in result.lower()
        assert "explain the service" in result.lower()

    def test_all_greeting_returns_unchanged(self):
        assert strip_leading_greeting("hi hello") == "hi hello"
