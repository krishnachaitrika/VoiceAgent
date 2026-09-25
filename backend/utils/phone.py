"""
utils/phone.py — enterprise-grade phone number normalization + validation.

WHY THIS EXISTS:
Before this module, every tool in brain/tools.py (save_lead,
confirm_booking_details, book_meeting) trusted whatever string GPT-4o put
in the "phone" argument and saved/read it back as-is. GPT-4o decides on
its own when it "has enough" of a number — there was no hard rule anywhere
in the code that a phone number must actually contain a valid count of
digits. In real calls this let leads/bookings get saved with 6, 7, 8 or 9
digit numbers (a caller trailing off, a barge-in cutting them off mid
number, a digit the model silently dropped), because nothing downstream
ever checked.

Prompt instructions alone ("ask for full number") are not reliable
enough for this at enterprise level — the LLM can still call the tool
early. The fix is a hard, deterministic check in code that runs on every
single tool call that touches a phone number, independent of what the
model "thinks" it heard.

WHAT THIS MODULE DOES:
1. Normalizes common ASR/telephony artifacts before counting digits:
   - Strips everything that isn't a digit (spaces, dashes, "+", brackets).
   - Expands the way Indian callers often *say* numbers, in case any of
     it leaks through STT as words instead of digits: "double" / "triple"
     before a digit, spelled-out digit words ("nine", "double six"), and
     "oh" used to mean zero.
   - Strips a recognized country code prefix (config.PHONE_NUMBER_COUNTRY_CODE)
     and/or a single leading trunk "0", so "+91 98765 43210", "0919876543210"
     and "9876543210" all normalize to the same 10 digits.
2. Validates the normalized result is EXACTLY
   config.PHONE_NUMBER_DIGIT_LENGTH digits long — not "at least", not
   "roughly" — and, if config.PHONE_NUMBER_ALLOWED_FIRST_DIGITS is set,
   that it starts with an allowed digit (Indian mobile numbers always
   start 6-9; a number starting 0-5 after normalization is almost always
   a mis-hearing, not a real mobile number).
3. Never raises — always returns a PhoneValidationResult so calling code
   can branch cleanly instead of wrapping every call site in try/except.

Nothing here is hardcoded: digit length, country code, and allowed first
digits all come from config.py / the environment, so this works for a
non-Indian deployment by just changing .env — no code change needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import config

# Spoken-number fragments that occasionally survive STT as words instead of
# digits (most often on noisy telephony audio where Sarvam falls back to
# transcribing what it heard literally). Order matters: "double"/"triple"
# must be resolved before the plain digit-word map below.
_DOUBLE_TRIPLE_RE = re.compile(
    r"\b(double|triple)\s+(zero|one|two|three|four|five|six|seven|eight|nine|oh|\d)\b",
    re.IGNORECASE,
)
_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9",
}
_DIGIT_WORD_RE = re.compile(
    r"\b(" + "|".join(_DIGIT_WORDS.keys()) + r")\b", re.IGNORECASE
)


@dataclass
class PhoneValidationResult:
    is_valid: bool
    normalized: str          # digits only, e.g. "9876543210" — "" if we
                              # couldn't extract anything at all
    digit_count: int
    reason: Optional[str]    # human-readable reason when is_valid is False,
                              # written to be read directly by the LLM so it
                              # knows exactly what to ask the caller for


def _expand_spoken_digits(raw: str) -> str:
    """Best-effort expansion of word-form digits before we strip to
    digits-only. Safe to run on already-numeric input — it's a no-op."""

    def _double_triple(match: re.Match) -> str:
        word, digit_word = match.group(1).lower(), match.group(2).lower()
        digit = _DIGIT_WORDS.get(digit_word, digit_word if digit_word.isdigit() else "")
        if not digit:
            return match.group(0)
        return digit * (2 if word == "double" else 3)

    text = _DOUBLE_TRIPLE_RE.sub(_double_triple, raw)
    text = _DIGIT_WORD_RE.sub(lambda m: _DIGIT_WORDS[m.group(1).lower()], text)
    return text


def normalize_and_validate_phone(raw: Optional[str]) -> PhoneValidationResult:
    """Normalize `raw` and validate it against config.PHONE_NUMBER_DIGIT_LENGTH.

    This is the single source of truth for "is this a real phone number" —
    every tool that saves or reads back a phone number must call this
    before touching the database, not just trust the LLM's argument.
    """
    if not raw or not raw.strip():
        return PhoneValidationResult(
            is_valid=False, normalized="", digit_count=0,
            reason="No phone number was provided at all.",
        )

    expanded = _expand_spoken_digits(raw)
    digits = re.sub(r"\D", "", expanded)

    expected_len = config.PHONE_NUMBER_DIGIT_LENGTH
    country_code = config.PHONE_NUMBER_COUNTRY_CODE.strip()

    # Strip a recognized country code prefix, e.g. "+91 9876543210" or
    # "919876543210" (12 digits) → "9876543210" (10 digits).
    if country_code and digits.startswith(country_code) and len(digits) == expected_len + len(country_code):
        digits = digits[len(country_code):]

    # Strip a single leading trunk "0" (common Indian landline/STD
    # convention), e.g. "09876543210" (11 digits) → "9876543210".
    if len(digits) == expected_len + 1 and digits.startswith("0"):
        digits = digits[1:]

    digit_count = len(digits)

    if digit_count < expected_len:
        return PhoneValidationResult(
            is_valid=False, normalized=digits, digit_count=digit_count,
            reason=(
                f"Only {digit_count} digit(s) were captured "
                f"('{digits}'), but a valid phone number needs exactly "
                f"{expected_len} digits. This is almost always the caller "
                f"trailing off, background noise, or being cut off "
                f"mid-number — NOT a real short number."
            ),
        )

    if digit_count > expected_len:
        return PhoneValidationResult(
            is_valid=False, normalized=digits, digit_count=digit_count,
            reason=(
                f"{digit_count} digits were captured ('{digits}'), which "
                f"is more than the expected {expected_len}. The caller may "
                f"have repeated part of the number or included extra "
                f"digits (like a landline STD code) that need to be "
                f"confirmed separately."
            ),
        )

    allowed_first = config.PHONE_NUMBER_ALLOWED_FIRST_DIGITS.strip()
    if allowed_first and digits[0] not in allowed_first:
        return PhoneValidationResult(
            is_valid=False, normalized=digits, digit_count=digit_count,
            reason=(
                f"'{digits}' has the right length but starts with "
                f"'{digits[0]}', which isn't a valid start digit for a "
                f"mobile number in this region. This is a strong sign a "
                f"digit was mis-heard."
            ),
        )

    return PhoneValidationResult(
        is_valid=True, normalized=digits, digit_count=digit_count, reason=None,
    )


def build_user_digit_stream(user_texts: list[str]) -> str:
    """Concatenate every digit actually present in a list of raw user
    transcript turns, in order, after the same spoken-word expansion used
    above. This is the deterministic "ground truth" digit stream for a
    call — used as a fidelity check so a phone number is only accepted if
    its digits genuinely trace back to something the caller said, not a
    plausible-looking number the model quietly padded/guessed to satisfy
    the length check (see phone_supported_by_history below for why the
    length check alone isn't enough)."""
    return "".join(re.sub(r"\D", "", _expand_spoken_digits(t)) for t in user_texts if t)


def phone_supported_by_history(normalized_phone: str, user_digit_stream: str) -> bool:
    """True if `normalized_phone` genuinely traces back to digits the
    caller said.

    WHY THIS EXISTS (real, confirmed bug — 2026-07-31 live call #1):
    The length check in normalize_and_validate_phone stops a number that's
    too short or too long, but it can't stop GPT-4o from fabricating extra
    digits to pad a garbled/fragmented capture into exactly the expected
    length. Checking the proposed number against what the caller's own
    turns actually contain catches this class of hallucination that a pure
    length/format check cannot.

    WHY IT'S A TWO-CHUNK CHECK, NOT A SIMPLE FULL SUBSTRING CHECK
    (real, confirmed follow-up bug — 2026-07-31 live call #2):
    A first version of this check required the whole number to appear as
    ONE contiguous run. That's too strict — it's completely normal for a
    caller to give part of the number in one turn and the rest in the
    next (or to correct just the back half after being asked to repeat),
    which is exactly what happened in call #2: "No, my mobile number is
    636999." + a follow-up "6 5 9 5" is a perfectly legitimate, correctly
    reconstructed 10-digit number ("6369996595") that a whole-string check
    rejected anyway, stranding the caller in an endless "please repeat"
    loop even after they'd already confirmed it correctly.

    So: accept the number if it appears as ONE contiguous run, OR if it
    can be split into exactly two contiguous pieces (each at least
    config.PHONE_FIDELITY_MIN_CHUNK_DIGITS long, so this can't be
    satisfied by trivially short 1-2 digit coincidences) that each
    individually appear in the stream. This still blocks wholesale
    fabrication — a hallucinated number essentially never decomposes into
    two real, meaningfully-sized chunks the caller actually said — while
    tolerating the very common "digits arrived in two turns" pattern.
    """
    if not normalized_phone:
        return False
    if normalized_phone in user_digit_stream:
        return True

    min_len = config.PHONE_FIDELITY_MIN_CHUNK_DIGITS
    n = len(normalized_phone)
    for split_at in range(min_len, n - min_len + 1):
        head, tail = normalized_phone[:split_at], normalized_phone[split_at:]
        if head in user_digit_stream and tail in user_digit_stream:
            return True
    return False