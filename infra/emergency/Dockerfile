# Emergency front: panel static and /instructions in one image.
# Build context is the repository root. The image is only a carrier of what
# node builds: deploy creates a stopped container and copies the static to
# /opt/vpn2-emergency on the host. The agent itself is the vpn2 package in the
# host venv, its units come with the deploy tree, so an emergency downloads
# nothing.

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

COPY --from=static /src/infra/emergency/vpn2_emergency/static /opt/vpn2-emergency/static
COPY --from=static /src/image.json /opt/vpn2-emergency/image.json

# docker create needs a command; the container is never started.
CMD ["/none"]
