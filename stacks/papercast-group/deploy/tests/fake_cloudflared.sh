#!/bin/sh
# A stand-in for cloudflared's quick tunnel, to check tunnel.sh's own logic (the address it
# parses, PCG_PUBLIC_URL set and put back, the time limit, down) where no real tunnel can come
# up. Prints what cloudflared prints, including the api.trycloudflare.com line that is not the
# address, then waits to be stopped.
#   PCG_CLOUDFLARED=deploy/tests/fake_cloudflared.sh bash deploy/tunnel.sh up --minutes 1
case "$1" in
    --version) echo "cloudflared version fake (tests)"; exit 0 ;;
esac
echo "2026-09-28T00:00:00Z INF Requesting new quick Tunnel on trycloudflare.com..."
echo "2026-09-28T00:00:00Z INF posting to https://api.trycloudflare.com/tunnel"
echo "2026-09-28T00:00:01Z INF +--------------------------------------------------------------------------------------------+"
echo "2026-09-28T00:00:01Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |"
echo "2026-09-28T00:00:01Z INF |  https://fake-words-for-tests.trycloudflare.com                                            |"
echo "2026-09-28T00:00:01Z INF +--------------------------------------------------------------------------------------------+"
exec sleep 3600
