/** @type {import('next').NextConfig} */

// /api/* used to be a bare next.config.js rewrite straight to the backend.
// That had two problems: it was hardcoded to "http://localhost:8000" (broke
// under `docker compose up`, where the frontend and backend are separate
// containers and "localhost:8000" inside the frontend container resolves
// to itself — every dashboard API call silently failed), and a rewrite has
// no way to attach a header, so it couldn't carry the auth credential the
// backend now requires (see backend/auth.py). app/api/[...path]/route.js
// replaces it with a Route Handler that reads BACKEND_INTERNAL_URL (a
// plain, non-NEXT_PUBLIC_ server-side env var) and attaches the credential
// server-side. NEXT_PUBLIC_API_URL — a separate, build-time-only variable
// once set alongside this in docker-compose.yml and
// k8s/05-frontend-deployment.yaml — was removed (VA-C3 fix): it was read
// by no source file in this app at all.
const nextConfig = {
  reactStrictMode: true,
};

module.exports = nextConfig;