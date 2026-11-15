#!/bin/bash
# Start the SOMEBODY relay server and a Cloudflare tunnel, print the phone link.
# Usage: ./run.sh [--port /dev/cu.usbmodemXXXX]
cd "$(dirname "$0")"

CLOUDFLARED="$(command -v cloudflared || echo "$PWD/bin/cloudflared")"
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"

python3 server.py "$@" &
SERVER_PID=$!
trap 'kill $SERVER_PID $TUNNEL_PID 2>/dev/null; exit' INT TERM EXIT
sleep 1

if [ -x "$CLOUDFLARED" ]; then
  TUNNEL_LOG="$(mktemp)"
  "$CLOUDFLARED" tunnel --url http://localhost:8080 >"$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!
  for _ in $(seq 1 30); do
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)"
    [ -n "$URL" ] && break
    sleep 1
  done
  echo
  if [ -n "$URL" ]; then
    echo "=============================================="
    echo "  Phone link (HTTPS): $URL"
    echo "=============================================="
  else
    echo "!! Tunnel did not start. See $TUNNEL_LOG"
  fi
else
  echo "!! cloudflared not found: no HTTPS link (camera won't work on iPhone)."
fi
[ -n "$LAN_IP" ] && echo "  Hotspot/LAN fallback: http://$LAN_IP:8080  (no camera)"
echo "  Fake events: curl localhost:8080/fake/RIGHT   Re-zero: curl localhost:8080/calibrate"
echo
wait $SERVER_PID
