"""
guardrails/patterns.py — shared detection patterns for the guardrails layer.

These are intentionally simple, fast, regex-based checks that run on every
turn (input AND output) BEFORE/AFTER the LLM call — see input_guardrail.py
and output_guardrail.py. They are a coarse safety net, not a substitute for
the behavioural rules already in brain/prompts.py; the system prompt tells
the model how to behave, this layer catches what slips through regardless
of what the model decides to do.
"""
import re

# ── PII patterns (for masking in logs / transcripts, not for blocking) ─────
CARD_NUMBER_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
CVV_CONTEXT_RE = re.compile(r"\b(cvv|cvc|security code)\b[^.]{0,20}?\b\d{3,4}\b", re.IGNORECASE)
BANK_ACCOUNT_CONTEXT_RE = re.compile(r"\b(account number|ifsc|routing number)\b", re.IGNORECASE)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# ── Jailbreak / prompt-injection patterns (input guardrail — block/flag) ───
JAILBREAK_PATTERNS = [
    re.compile(r"ignore (all|any|the) (previous|prior|above) instructions?", re.IGNORECASE),
    re.compile(r"you are now (in )?(dan|developer mode|jailbreak)", re.IGNORECASE),
    re.compile(r"disregard your (system prompt|instructions|guidelines)", re.IGNORECASE),
    re.compile(r"pretend (you are|to be) (not )?an? ai", re.IGNORECASE),
    re.compile(r"reveal your (system prompt|instructions|prompt)", re.IGNORECASE),
    re.compile(r"repeat (the )?(words|text) above", re.IGNORECASE),
]

# ── Compliance — things the agent must never promise/collect (output guard) ─
FORBIDDEN_OUTPUT_PATTERNS = [
    # System-prompt / instruction leaks — covers common phrasings the model
    # might slip into once a jailbreak partially succeeds, not just the
    # single "my system prompt is..." template originally checked for.
    re.compile(r"\bmy (system prompt|instructions?|guidelines) (are|is)\b", re.IGNORECASE),
    re.compile(r"\bmy (system prompt|instructions?)\b", re.IGNORECASE),
    re.compile(r"\bi (was|am) instructed to\b", re.IGNORECASE),
    re.compile(r"\bi('?m| am) not (allowed|supposed) to (say|reveal|share|tell)\b", re.IGNORECASE),
    re.compile(r"\b(here('?s| is) my|here('?s| is) the) (system prompt|instructions)\b", re.IGNORECASE),
    re.compile(r"\bsystem prompt\b", re.IGNORECASE),
    re.compile(r"\byou are an? (ai|virtual)?\s*assistant (called|named)\b", re.IGNORECASE),
    re.compile(r"\bignoring (all|any|the) (previous|prior|above) instructions?\b", re.IGNORECASE),
    re.compile(r"\bas (an? )?(ai language model|large language model|llm)\b.{0,30}\binstruct", re.IGNORECASE),

    # Sensitive-data leaks — several common phrasings for card/CVV/SSN/bank
    # details, not just the original "card number ... is" template.
    re.compile(r"\b(card|credit card|debit card)\s*number\b.{0,15}\b(is|are)\b", re.IGNORECASE),
    re.compile(r"\b(cvv|cvc|security code)\b.{0,20}\b(is|are)\b", re.IGNORECASE),
    re.compile(r"\b(cvv|cvc)\s*(number\s*)?[:\-]?\s*\d{3,4}\b", re.IGNORECASE),
    re.compile(r"\bssn\b.{0,15}\b(is|are)\b", re.IGNORECASE),
    re.compile(r"\bsocial security number\b.{0,15}\b(is|are)\b", re.IGNORECASE),
    re.compile(r"\bsocial security number\b", re.IGNORECASE),
    re.compile(r"\b(bank\s*)?account number\b.{0,15}\b(is|are)\b", re.IGNORECASE),
    re.compile(r"\brouting number\b.{0,15}\b(is|are)\b", re.IGNORECASE),
]
