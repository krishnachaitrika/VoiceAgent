/**
 * POST /api/auth/login — forward credentials to the backend, set the cookie.
 *
 * The password is verified by the backend against an Argon2id hash in the
 * `users` table (backend/services/user_auth.py). This route only relays the
 * attempt and, on success, stores the returned token in an httpOnly cookie.
 *
 * The password is never logged here, never stored, and never returned to the
 * browser — only the signed session token is.
 */
import { NextResponse } from "next/server";

import { SESSION_COOKIE, sessionCookieOptions } from "@/lib/session";

const BACKEND_URL = process.env.BACKEND_INTERNAL_URL || "http://localhost:8000";

export async function POST(request) {
  let username = "";
  let password = "";
  try {
    ({ username = "", password = "" } = await request.json());
  } catch {
    return NextResponse.json({ detail: "Invalid request body." }, { status: 400 });
  }

  let backendResponse;
  try {
    backendResponse = await fetch(`${BACKEND_URL}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
      cache: "no-store",
    });
  } catch {
    // Distinguished from a rejected login on purpose: "the backend is down"
    // and "your password is wrong" need completely different responses from
    // whoever is reading the screen.
    return NextResponse.json(
      { detail: "Could not reach the server. Is the backend running?" },
      { status: 503 },
    );
  }

  const body = await backendResponse.json().catch(() => ({}));

  if (!backendResponse.ok) {
    // Pass the backend's message through unchanged. It already decides what
    // is safe to reveal: one generic string for every credential failure, so
    // an attacker cannot tell "no such user" from "wrong password" and
    // enumerate accounts — with lockout the deliberate exception, because a
    // colleague locked out needs to know why.
    return NextResponse.json(
      { detail: body.detail || "Sign-in failed." },
      { status: backendResponse.status },
    );
  }

  const response = NextResponse.json({
    ok: true,
    username: body.username,
    role: body.role,
  });
  response.cookies.set(SESSION_COOKIE, body.token, sessionCookieOptions());
  return response;
}