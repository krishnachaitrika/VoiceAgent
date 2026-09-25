"""Tests for VA-B2: Twilio webhook signature validation + stream-token
signing/verification."""
from twilio.request_validator import RequestValidator

import config
from twilio_auth import (
    validate_twilio_signature,
    issue_stream_token,
    verify_stream_token,
)


class TestValidateTwilioSignature:
    def test_accepts_a_genuinely_valid_signature(self):
        url = "https://example.ngrok.io/incoming-call"
        params = {"From": "+15551234567", "CallSid": "CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
        real_signature = RequestValidator(config.TWILIO_AUTH_TOKEN).compute_signature(url, params)
        assert validate_twilio_signature(url, params, real_signature) is True

    def test_rejects_a_forged_signature(self):
        url = "https://example.ngrok.io/incoming-call"
        params = {"From": "+15551234567", "CallSid": "CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}
        assert validate_twilio_signature(url, params, "not-a-real-signature") is False

    def test_rejects_missing_signature(self):
        url = "https://example.ngrok.io/incoming-call"
        assert validate_twilio_signature(url, {}, None) is False

    def test_fails_closed_when_auth_token_unset(self, monkeypatch):
        monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
        url = "https://example.ngrok.io/incoming-call"
        params = {"From": "+15551234567"}
        real_signature = RequestValidator("some-token").compute_signature(url, params)
        assert validate_twilio_signature(url, params, real_signature) is False

    def test_signature_is_url_specific(self):
        params = {"From": "+15551234567"}
        signature = RequestValidator(config.TWILIO_AUTH_TOKEN).compute_signature(
            "https://example.ngrok.io/incoming-call", params
        )
        # Same signature must not validate against a different URL.
        assert validate_twilio_signature(
            "https://attacker.example.com/incoming-call", params, signature
        ) is False


class TestStreamToken:
    def test_freshly_issued_token_verifies(self):
        token, expiry = issue_stream_token("CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        assert verify_stream_token("CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", str(expiry), token) is True

    def test_token_rejected_for_a_different_call_sid(self):
        token, expiry = issue_stream_token("CAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        assert verify_stream_token("CAbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", str(expiry), token) is False

    def test_tampered_token_is_rejected(self):
        token, expiry = issue_stream_token("CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        assert verify_stream_token("CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", str(expiry), tampered) is False

    def test_expired_token_is_rejected(self):
        token, expiry = issue_stream_token("CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", ttl_seconds=-10)
        assert verify_stream_token(
            "CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", str(expiry), token
        ) is False

    def test_missing_fields_are_rejected(self):
        assert verify_stream_token("", "", "") is False
        assert verify_stream_token("CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "not-a-number", "abc") is False

    def test_fails_closed_when_auth_token_unset(self, monkeypatch):
        token, expiry = issue_stream_token("CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
        assert verify_stream_token("CAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", str(expiry), token) is False
