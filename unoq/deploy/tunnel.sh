#!/bin/bash
# Run a Cloudflare quick tunnel to the talking screen and publish its URL to tunnel_url.txt.
# - Waits until the internet (DNS) works, so it doesn't fail while Wi-Fi is still coming up.
# - Only real tunnel links (word-word-word.trycloudflare.com) count; error lines mentioning
#   api.trycloudflare.com are ignored.
# - If Cloudflare forgets the tunnel ("Tunnel not found", e.g. after a long Wi-Fi drop), exit so
#   systemd restarts us with a fresh tunnel. The /go link always points at the current one.
cd "$(dirname "$0")/.."
rm -f tunnel_url.txt
for i in $(seq 1 60); do
  getent hosts api.trycloudflare.com >/dev/null 2>&1 && break
  [ "$i" = 1 ] && echo "Waiting for the internet before starting the tunnel..."
  sleep 2
done
gone=0
./bin/cloudflared tunnel --no-autoupdate --url http://localhost:8080 2>&1 | while IFS= read -r line; do
  echo "$line"
  url=$(echo "$line" | grep -oE 'https://[a-z0-9]+(-[a-z0-9]+)+\.trycloudflare\.com' | grep -v '^https://api\.' | head -1)
  if [ -n "$url" ]; then echo "$url" > tunnel_url.txt; echo "TUNNEL URL: $url"; gone=0; fi
  if echo "$line" | grep -q "Tunnel not found"; then
    gone=$((gone + 1))
    if [ "$gone" -ge 3 ]; then echo "Tunnel expired on Cloudflare's side; restarting for a fresh one"; pkill -f "cloudflared tunnel --no-autoupdate --url http://localhost:8080"; exit 1; fi
  fi
done
exit 1
