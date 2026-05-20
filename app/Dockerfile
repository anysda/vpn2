# --- builder ---------------------------------------------------------------
FROM node:22-slim AS builder

RUN corepack enable && corepack prepare pnpm@10 --activate

WORKDIR /app

COPY package.json pnpm-lock.yaml .npmrc ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

# --- runtime ---------------------------------------------------------------
FROM node:22-slim AS runtime

RUN apt-get update \
  && apt-get install -y --no-install-recommends procps ca-certificates curl wireguard-tools openssl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV NODE_ENV=production
ENV PORT=51821

COPY --from=builder /app/.output ./.output
COPY --from=builder /app/server/database/migrations ./server/database/migrations

# h3's useSession bakes `DEFAULT_COOKIE = { secure: true, ... }` into the
# bundle. We can't override this through runtimeConfig (defu drops the
# extra keys at module init time). Rewrite the bundled output so that the
# session cookie isn't Secure — needed for HTTP-only deploys. To re-enable
# Secure for HTTPS prod, override via NUXT_SESSION_COOKIE_SECURE=true at
# runtime (panel reads env in server/plugins/init.ts).
RUN sed -i 's/^  secure: true,$/  secure: false,/' \
  /app/.output/server/chunks/nitro/nitro.mjs \
  && grep -c '^  secure: false,$' /app/.output/server/chunks/nitro/nitro.mjs

EXPOSE 51821

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:51821/api/version || exit 1

CMD ["node", ".output/server/index.mjs"]
