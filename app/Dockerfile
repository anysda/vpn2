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

# iproute2 нужен для `ip -6 addr replace` в writeWgServerConfig(): wg syncconf
# адреса интерфейса не трогает, поэтому ULA fd66:66::1/64 ставится отдельной
# командой. Без пакета она падает с `spawn ip ENOENT`, ошибка глотается catch'ем
# и ULA молча не появляется на wg0 — телега продолжает висеть в таймаутах.
RUN apt-get update \
  && apt-get install -y --no-install-recommends procps ca-certificates curl wireguard-tools iproute2 openssl util-linux \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV NODE_ENV=production
ENV PORT=51821

COPY --from=builder /app/.output ./.output
COPY --from=builder /app/server/database/migrations ./server/database/migrations
COPY --from=builder /app/scripts/migrate.mjs ./scripts/migrate.mjs

EXPOSE 51821

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-51821}/api/version" || exit 1

# TODO (security audit H10): сейчас контейнер бежит как root + bind-mount
# /etc/anysda rw + CAP_NET_ADMIN — RCE в Nuxt = full disclosure всех секретов
# (admin-password.txt, tgbot-secret.txt, WG/IKEv2 ключей). Перевод на USER node
# требует переноса wg-команд в host-side helper (через `pkexec`/привилегированный
# socket) и точечного :ro-mount файлов из /etc/anysda. Делается отдельным PR
# с обязательным прогоном test.md L4-routing на стенде.
CMD ["node", ".output/server/index.mjs"]
