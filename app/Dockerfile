# --- builder ---------------------------------------------------------------
FROM mirror.gcr.io/library/node:24-slim AS builder

RUN corepack enable && corepack prepare pnpm@10 --activate

WORKDIR /app

COPY package.json pnpm-lock.yaml .npmrc ./
RUN pnpm install --frozen-lockfile

COPY . .
RUN pnpm build

# --- runtime ---------------------------------------------------------------
FROM mirror.gcr.io/library/node:24-slim AS runtime

# VPN utilities and PKI stay on the host; util-linux is used by deploy migration.
RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl util-linux \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV NODE_ENV=production
ENV PORT=51821

COPY --from=builder /app/.output ./.output
COPY --from=builder /app/server/database/migrations ./server/database/migrations
COPY --from=builder /app/scripts/migrate.mjs ./scripts/migrate.mjs
COPY --from=builder /app/scripts/control-probe.mjs ./scripts/control-probe.mjs

USER 10001:10001

EXPOSE 51821

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-51821}/api/version" || exit 1

# Deploy maps the dedicated host UID and authorizes it at the Unix socket.
CMD ["node", ".output/server/index.mjs"]
