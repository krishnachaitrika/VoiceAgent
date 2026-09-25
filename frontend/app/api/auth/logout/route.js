/**
 * POST /api/auth/logout — clear the session cookie.
 *
 * maxAge: 0 with the same attributes as the original. A cookie is only
 * replaced when path and domain match exactly, so reusing sessionCookieOptions
 * is what makes the clear actually take effect rather than quietly adding a
 * second cookie the browser keeps sending.
 */
import { NextResponse } from "next/server";

import { SESSION_COOKIE, sessionCookieOptions } from "@/lib/session";

export async function POST() {
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, "", { ...sessionCookieOptions(), maxAge: 0 });
  return response;
}