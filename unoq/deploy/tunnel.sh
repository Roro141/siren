#!/bin/bash
# Run a Cloudflare quick tunnel to the talking screen and publish its URL to tunnel_url.txt.
# If Cloudflare forgets the tunnel (e.g. after a long Wi-Fi drop: "Tunnel not found"),
# exit so systemd restarts us with a fresh tunnel and a new URL (the /go link follows it).
cd "$(dirname "$0")/.."
rm -f tunnel_url.txt
gone=0
./bin/cloudflared tunnel --no-autoupdate --url http://localhost:8080 2>&1 | while IFS= read -r line; do
  echo "$line"
  url=$(echo "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com')
  if [ -n "$url" ]; then echo "$url" > tunnel_url.txt; echo "TUNNEL URL: $url"; gone=0; fi
  if echo "$line" | grep -q "Tunnel not found"; then
    gone=$((gone + 1))
    if [ "$gone" -ge 3 ]; then echo "Tunnel expired on Cloudflare's side; restarting for a fresh one"; pkill -P $$ cloudflared; pkill -f "cloudflared tunnel --no-autoupdate --url http://localhost:8080"; exit 1; fi
  fi
done
exit 1
