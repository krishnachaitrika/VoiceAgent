/**
 * lib/session.js — dashboard session cookie (VA-T-002).
 *
 * WHERE THE PASSWORD IS ACTUALLY CHECKED
 *
 * Not here. Password hashes live in Postgres, which only the backend talks
 * to, so the backend verifies credentials (see backend/api/auth_session.py
 * and backend/services/user_auth.py). This module only handles the token the
 * backend issues afterwards.
 *
 *     browser → /api/auth/login → backend POST /api/auth/login
 *                                      ↓ Argon2id verify against `users`
 *                                 ← {token, expires_at, username}
 *          ← httpOnly cookie ←
 *
 * The frontend never sees a password after forwarding it, never sees a hash,
 * and stores only a token in a cookie JavaScript cannot read.
 *
 * WHY THE TOKEN IS VERIFIED HERE RATHER THAN ASKING THE BACKEND
 *
 * Middleware runs on EVERY request, including ordinary navigation. A backend
 * round trip per request would add latency to every page load for no gain.
 * The token is an HMAC the backend signed with DASHBOARD_SESSION_SECRET, so
 * holding the same secret lets this verify it locally in microseconds.
 *
 * That secret must be IDENTICAL in backend .env and frontend/.env. It is not
 * a password and nobody types it — it is a signing key.
 *
 * HONEST LIMITATION
 *
 * A signed token cannot be revoked before it expires. Deactivating a user
 * stops the next login but does not kill a live session until
 * DASHBOARD_SESSION_TTL_HOURS elapses. Rotating DASHBOARD_SESSION_SECRET
 * invalidates every session at once and is the lever for "end it now".
 *
 * Uses Web Crypto rather than node:crypto so the same code runs in Next.js
 * middleware, which executes on the Edge runtime.
 */

export const SESSION_COOKIE = "va_dashboard_session";

const SESSION_SECRET = process.env.DASHBOARD_SESSION_SECRET || "";
const TTL_HOURS = Number(process.env.DASHBOARD_SESSION_TTL_HOURS || 12);

const encoder = new TextEncoder();

/** Is session verification configured at all? */
export function isAuthConfigured() {
  return SESSION_SECRET.length > 0;
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

async function sign(payload) {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(SESSION_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return toHex(await crypto.subtle.sign("HMAC", key, encoder.encode(payload)));
}

/**
 * Constant-time comparison.
 *
 * A plain === returns early at the first differing character, so response
 * timing leaks the signature's prefix and it can be recovered one character
 * at a time. Comparing every character makes the duration independent of
 * where the mismatch is.
 */
function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let i = 0; i < a.length; i++) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

/**
 * Validate a backend-issued token: "<userId>.<username>.<expiry>.<hmac>".
 * Returns its claims, or null if the signature is wrong or it has expired.
 */
export async function verifySessionToken(token) {
  if (!token || !isAuthConfigured()) return null;

  const parts = token.split(".");
  if (parts.length !== 4) return null;

  const [userId, username, expiresRaw, signature] = parts;
  const payload = `${userId}.${username}.${expiresRaw}`;

  if (!timingSafeEqual(signature, await sign(payload))) return null;

  const expiresAt = Number(expiresRaw);
  if (!Number.isFinite(expiresAt) || Date.now() / 1000 >= expiresAt) return null;

  return { userId, username, expiresAt };
}

/** Cookie attributes. Shared so the set and clear paths cannot drift apart. */
export function sessionCookieOptions() {
  return {
    httpOnly: true, // unreadable from JavaScript, so XSS cannot steal it
    sameSite: "lax", // blocks cross-site form posts, keeps normal navigation
    // Secure needs HTTPS, which would break a plain-HTTP local run — so it
    // follows NODE_ENV rather than being hardcoded either way.
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: TTL_HOURS * 60 * 60,
  };
}