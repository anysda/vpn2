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
  && apt-get install -y --no-install-recommends procps ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV NODE_ENV=production
ENV PORT=51821

COPY --from=builder /app/.output ./.output
COPY --from=builder /app/server/database/migrations ./server/database/migrations

EXPOSE 51821

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:51821/api/version || exit 1

CMD ["node", ".output/server/index.mjs"]
