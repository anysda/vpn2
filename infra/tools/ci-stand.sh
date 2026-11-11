#!/bin/bash
# Стенд CI переехал в infra/tests; путь оставлен ради workflows.
exec "$(dirname "$0")/../tests/ci-stand.sh" "$@"
