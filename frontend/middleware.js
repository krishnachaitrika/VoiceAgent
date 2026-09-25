/**
 * middleware.js — gate every dashboard route behind a session (VA-T-002).
 *
 * Runs before any page or API route is reached, so there is no window in
 * which an unauthenticated request touches the proxy — and no way to forget
 * the check on a page added later. That last point is why this is middleware
 * rather than a guard inside each page: a new page under app/ is protected the
 * moment it exists, with nothing to remember.
 *
 * WHAT IT PROTECTS
 *
 * Everything except the login page itself, the login API, and Next's own
 * static assets. In particular /api/[...path] — the proxy that attaches
 * DASHBOARD_ADMIN_KEY — which is the actual hole: without this, a request
 * straight to /api/dashboard/calls returned transcripts and caller PII to
 * anyone who could reach the host, no browser session involved.
 *
 * FAIL CLOSED
 *
 * With no DASHBOARD_SESSION_PASSWORD configured, every request is redirected
 * to /login, which then explains that the dashboard is not configured. The
 * alternative — treating "no password" as "no auth required" — is how an
 * unprotected console reaches production: it works perfectly right up until
 * someone points a public hostname at it.
 *
 * The backend logs the same condition at startup (config_validation.py), so
 * the mistake is visible in two places before anyone tries to use it.
 */
import { NextResponse } from "next/server";

import { SESSION_COOKIE, isAuthConfigured, verifySessionToken } from "@/lib/session";

// Paths reachable without a session. Kept deliberately short — every entry is
// a hole someone has to justify.
const PUBLIC_PATHS = ["/login", "/api/auth/login", "/api/auth/logout"];

function isPublic(pathname) {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

export async function middleware(request) {
  const { pathname, search } = request.nextUrl;

  if (isPublic(pathname)) {
    return NextResponse.next();
  }

  const token = request.cookies.get(SESSION_COOKIE)?.value;
  // verifySessionToken returns the token's claims (or null), not a boolean —
  // the identity is available here if a future check needs it (per-tenant
  // routing, role gating), without a second parse.
  if (isAuthConfigured() && (await verifySessionToken(token))) {
    return NextResponse.next();
  }

  // An API call gets 401 rather than a redirect. Returning the login page's
  // HTML to fetch() would surface as a JSON parse error in the dashboard,
  // which tells whoever is debugging it nothing about what actually happened.
  if (pathname.startsWith("/api/")) {
    return NextResponse.json(
      {
        detail: isAuthConfigured()
          ? "Not authenticated. Sign in to the dashboard."
          : "Dashboard sessions are not configured (DASHBOARD_SESSION_SECRET).",
      },
      { status: 401 },
    );
  }

  // Carry the original destination so signing in lands where the user meant
  // to go rather than dumping everyone on the dashboard home.
  const loginUrl = new URL("/login", request.url);
  if (pathname !== "/") {
    loginUrl.searchParams.set("next", `${pathname}${search || ""}`);
  }
  return NextResponse.redirect(loginUrl);
}

export const config = {
  /*
   * Everything except Next's internals and static files.
   *
   * _next/static and _next/image are build output with no user data.
   * favicon.ico and friends are fetched by the browser before any redirect
   * could apply, so excluding them avoids pointless middleware invocations on
   * every page load.
   */
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)"],
};