#!/bin/bash
# Run a Cloudflare quick tunnel to the local server and publish its URL to tunnel_url.txt
cd "$(dirname "$0")/.."
rm -f tunnel_url.txt
./bin/cloudflared tunnel --no-autoupdate --url http://localhost:8080 2>&1 | while IFS= read -r line; do
  echo "$line"
  url=$(echo "$line" | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com')
  if [ -n "$url" ]; then echo "$url" > tunnel_url.txt; echo "TUNNEL URL: $url"; fi
done
