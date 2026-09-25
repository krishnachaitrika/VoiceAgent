"""
guardrails/output_guardrail.py — runs on every GPT-4o response BEFORE it is
spoken to the caller or written to the transcript. Pairs with
input_guardrail.py to form the "Separate Guardrails" middleware layer.

Checks:
1. System-prompt leak — the model should never reveal or quote its own
   instructions, even under a jailbreak. If a leak pattern is detected, we
   replace the whole response with a safe fallback line instead of just
   masking a fragment (a partial leak is still a leak).
2. PII masking for STORAGE — mirrors input_guardrail: mask anything that
   looks like a card number or SSN before it's written to the transcript,
   even if the model echoed it back.
"""
import logging
from dataclasses import dataclass, field
from typing import List

from guardrails.patterns import FORBIDDEN_OUTPUT_PATTERNS, CARD_NUMBER_RE, SSN_RE

logger = logging.getLogger(__name__)

SAFE_FALLBACK_RESPONSE = "I can't share that, but I'm happy to help with anything about our services."


@dataclass
class OutputGuardrailResult:
    speak_text: str      # what the agent should actually say
    storage_text: str    # PII-masked version for transcripts/logs
    flags: List[str] = field(default_factory=list)


def check_output(call_id: str, response_text: str) -> OutputGuardrailResult:
    flags: List[str] = []
    speak_text = response_text

    for pattern in FORBIDDEN_OUTPUT_PATTERNS:
        if pattern.search(response_text):
            flags.append("system_prompt_leak_blocked")
            logger.warning(f"[{call_id}] Guardrail: blocked a system-prompt leak in output")
            speak_text = SAFE_FALLBACK_RESPONSE
            break

    storage_text = speak_text
    if CARD_NUMBER_RE.search(storage_text):
        storage_text = CARD_NUMBER_RE.sub("[CARD NUMBER REDACTED]", storage_text)
        flags.append("card_number_masked_output")
    if SSN_RE.search(storage_text):
        storage_text = SSN_RE.sub("[SSN REDACTED]", storage_text)
        flags.append("ssn_masked_output")

    if flags:
        logger.info(f"[{call_id}] Output guardrail flags: {flags}")

    return OutputGuardrailResult(speak_text=speak_text, storage_text=storage_text, flags=flags)
