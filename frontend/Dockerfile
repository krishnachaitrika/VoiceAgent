FROM node:20-slim AS builder
WORKDIR /app

# NEXT_PUBLIC_* vars are baked into the JS bundle at `next build` time —
# setting them as a container `environment:` at runtime (docker-compose)
# would NOT work, since the build already happened. They must come in as
# build ARGs instead, which docker-compose passes via `build: args:`.
# NEXT_PUBLIC_API_URL removed (VA-C3 fix) — read by no source file; the
# frontend only ever talks to the backend through
# app/api/[...path]/route.js's BACKEND_INTERNAL_URL.
ARG NEXT_PUBLIC_AGENT_NAME=Velu
ARG NEXT_PUBLIC_COMPANY_NAME=Technozis
ARG NEXT_PUBLIC_APP_VERSION=3.0.0
ENV NEXT_PUBLIC_AGENT_NAME=$NEXT_PUBLIC_AGENT_NAME
ENV NEXT_PUBLIC_COMPANY_NAME=$NEXT_PUBLIC_COMPANY_NAME
ENV NEXT_PUBLIC_APP_VERSION=$NEXT_PUBLIC_APP_VERSION

COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json

# Run as a non-root user (VA security fix) — the app only binds to 3000,
# so no root / CAP_NET_BIND_SERVICE is needed. UID/GID 1001 is what
# k8s/05-frontend-deployment.yaml's securityContext (runAsUser/runAsGroup)
# expects for this image.
RUN groupadd --gid 1001 appuser \
    && useradd --uid 1001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

EXPOSE 3000
USER appuser
CMD ["npm", "start"]
