# Emergency front: agent, panel static and /instructions in one image.
# Build context is the repository root. The image is only a carrier: deploy
# creates a stopped container, copies /opt/vpn2-emergency to the host and runs
# the agent as a systemd unit, so an emergency downloads nothing.

# --- static ------------------------------------------------------------------
FROM mirror.gcr.io/library/node:26-slim AS static

RUN npm install -g pnpm@12

WORKDIR /src/app

COPY app/package.json app/pnpm-lock.yaml app/pnpm-workspace.yaml app/.npmrc ./
RUN pnpm install --frozen-lockfile

COPY app/ ./
RUN pnpm emergency:build

ARG VPN2_VERSION=dev
ARG VPN2_REVISION=unknown
RUN node -e 'const [version, revision] = process.argv.slice(1); \
  process.stdout.write(JSON.stringify({ version, revision }) + "\n")' \
  "$VPN2_VERSION" "$VPN2_REVISION" > /src/image.json

# --- carrier -----------------------------------------------------------------
FROM scratch

ARG VPN2_VERSION=dev
ARG VPN2_REVISION=unknown
LABEL vpn2.component=emergency \
      org.opencontainers.image.version=$VPN2_VERSION \
      org.opencontainers.image.revision=$VPN2_REVISION

# Пакет vpn2 целиком: агент зовётся как `python3 -m vpn2.emergency`, общие
# модули пакета лежат рядом, на хост ничего не ставится.
COPY infra/vpn2/src/vpn2 /opt/vpn2-emergency/vpn2
COPY --from=static /src/infra/emergency/vpn2_emergency/static /opt/vpn2-emergency/vpn2/emergency/static
COPY infra/emergency/install-hy2.sh infra/emergency/vpn2-emergency-hy2.service \
     infra/emergency/vpn2-emergency-hy2-probe.service \
     infra/emergency/vpn2-emergency-snapshot.service /opt/vpn2-emergency/
COPY --from=static /src/image.json /opt/vpn2-emergency/image.json

# docker create needs a command; the container is never started.
CMD ["/none"]
