"""
Trigger the AI agent to call YOU (outbound call).

Usage:
  cd backend
  python scripts/make_test_call.py +91XXXXXXXXXX

Requirements:
  - Your number must be a "Verified Caller ID" in Twilio Console
    (Phone Numbers -> Manage -> Verified Caller IDs) if you're on a trial account.
  - NGROK_URL in .env must be set and ngrok must be running
    (ngrok http 8000) so Twilio can reach your local /incoming-call webhook.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from twilio.rest import Client
import config


def make_call(to_number: str) -> None:
    if not config.NGROK_URL:
        print("❌ NGROK_URL is not set in .env. Start ngrok and set it first.")
        return

    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)

    webhook_url = f"{config.NGROK_URL.rstrip('/')}/incoming-call"

    print(f"📞 Calling {to_number} from {config.TWILIO_PHONE_NUMBER} ...")
    print(f"   Webhook: {webhook_url}")

    call = client.calls.create(
        to=to_number,
        from_=config.TWILIO_PHONE_NUMBER,
        url=webhook_url,
        method="POST",
    )

    print(f"✅ Call triggered. SID: {call.sid}")
    print("   Watch your uvicorn terminal logs, and answer your phone!")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/make_test_call.py +91XXXXXXXXXX")
        sys.exit(1)

    target_number = sys.argv[1]
    make_call(target_number)
