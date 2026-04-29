#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${PORTAL_BUCKET:-}" ]]; then
  echo "Defina PORTAL_BUCKET (ex.: export PORTAL_BUCKET=meu-bucket)"
  exit 1
fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
aws s3 sync "$ROOT/frontend/" "s3://${PORTAL_BUCKET}/" --delete
echo "Sync concluído para s3://${PORTAL_BUCKET}/"
