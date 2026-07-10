#!/bin/sh
set -e

missing=""
for var in API_TOKEN SMTP_HOST SMTP_USERNAME SMTP_PASSWORD SMTP_FROM_DEFAULT; do
  eval "value=\$$var"
  if [ -z "$value" ]; then
    missing="$missing $var"
  fi
done

if [ -n "$missing" ]; then
  echo "Missing required environment variables:$missing"
  echo ""
  cat /app/API.md
  exit 1
fi

exec python3 /app/app.py
