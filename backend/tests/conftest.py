"""
Deterministic env vars for the whole test session, set BEFORE any test
module imports app code (config.py's load_dotenv() never overrides a
variable already present in os.environ, so these win over a real local
.env file too — every test run, local or CI, sees the same values).
"""
import os

os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-twilio-auth-token")
os.environ.setdefault("DASHBOARD_API_KEY", "test-dashboard-api-key")
os.environ.setdefault("DASHBOARD_ADMIN_KEY", "test-dashboard-admin-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
