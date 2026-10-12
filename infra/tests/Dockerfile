FROM mirror.gcr.io/library/node:24-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends age expect sqlite3 wireguard-tools openssl python3 \
    && rm -rf /var/lib/apt/lists/*
ENV VPN2_BACKUP_TEST_CONTAINER=1
CMD ["python3", "/repo/infra/tests/backup-keys.py"]
