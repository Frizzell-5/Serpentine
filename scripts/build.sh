#!/bin/sh
# Copies the static site into dist/ for Cloudflare Pages (functions/ is picked up from the repo root).
set -e
cd "$(dirname "$0")/.."
rm -rf dist
mkdir -p dist/engine dist/docs
cp index.html app.html app.css app.js commands.js cloud.js plate.js worker.js dist/
cp engine/serpentine.py dist/engine/
cp -R docs/img dist/docs/img
cp docs/screenshot.png dist/docs/ 2>/dev/null || true
cat > dist/_headers <<'H'
/*
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=()
  X-Frame-Options: DENY
H
echo "Built dist/"
