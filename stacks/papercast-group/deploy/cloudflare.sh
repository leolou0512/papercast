#!/usr/bin/env bash
# Public access through Cloudflare, on perov: the tunnel, and the hub switched to Cloudflare Access
# login. The domain owner makes the tunnel and the Access application in his dashboard (README.md
# "Cloudflare"); this script only uses what he sends: the tunnel token, the team name, the AUD tag.
#
#   bash deploy/cloudflare.sh tunnel                 start the tunnel (token in ~/papercast-group/tunnel.token, 600)
#   bash deploy/cloudflare.sh login --team T --aud A --url https://papercast.virtualatoms.org [--admin EMAIL]
#                                                    the hub uses Cloudflare Access login at that address
#   bash deploy/cloudflare.sh local                  back to the hub's own invite links (tunnel stays up)
#   bash deploy/cloudflare.sh status
set -euo pipefail
. "$(dirname "$0")/lib.sh"
H=${PCG_HOME:-$HOME/papercast-group}
E=$H/hub.env
UNITS=$HOME/.config/systemd/user
PY=$H/venv/bin/python
APP=$H/app/papercast-group

cmd=${1:-status}; shift || true
case "$cmd" in
tunnel)
    tok=$H/tunnel.token
    [ -s "$tok" ] || die "no token at $tok: umask 077; cat > $tok (paste, Enter, Ctrl-D)"
    chmod 600 "$tok"
    [ -x "$HOME/.local/bin/cloudflared" ] || die "no cloudflared in ~/.local/bin (deploy/tunnel.sh installs it)"
    mkdir -p "$UNITS"
    sed "s#@H@#$H#g" "$APP/deploy/systemd/pcg-tunnel.service" > "$UNITS/pcg-tunnel.service"
    systemctl --user daemon-reload
    systemctl --user enable --now pcg-tunnel.service
    sleep 8
    if journalctl --user -u pcg-tunnel -n 50 --no-pager | grep -q "Registered tunnel connection"; then
        echo "tunnel up: $(journalctl --user -u pcg-tunnel -n 50 --no-pager | grep -c 'Registered tunnel connection') connection(s) to Cloudflare"
    else
        journalctl --user -u pcg-tunnel -n 15 --no-pager
        die "the tunnel did not register (above)"
    fi
    ;;
login)
    team= aud= url= admin=
    while [ $# -gt 0 ]; do
        case "$1" in
        --team) team=${2%.cloudflareaccess.com}; shift 2 ;;
        --aud) aud=$2; shift 2 ;;
        --url) url=${2%/}; shift 2 ;;
        --admin) admin=$2; shift 2 ;;
        *) die "unknown option $1" ;;
        esac
    done
    [ -n "$team" ] && [ -n "$aud" ] && [ -n "$url" ] || die "login needs --team, --aud and --url"
    case "$url" in https://*) ;; *) die "--url must be https://..." ;; esac
    code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "https://$team.cloudflareaccess.com/cdn-cgi/access/certs")
    [ "$code" = 200 ] || die "https://$team.cloudflareaccess.com/cdn-cgi/access/certs answered $code: is the team name right?"
    set_env "$E" PCG_AUTH cf-access
    set_env "$E" PCG_CF_TEAM "$team"
    set_env "$E" PCG_CF_AUD "$aud"
    set_env "$E" PCG_PUBLIC_URL "$url"
    if [ -n "$admin" ]; then
        cur=$(grep '^PCG_ADMIN_EMAILS=' "$E" | cut -d= -f2- || true)
        case ",$cur," in *",$admin,"*) ;; *) set_env "$E" PCG_ADMIN_EMAILS "${cur:+$cur,}$admin" ;; esac
        (cd "$APP" && set -a && . "$E" && set +a && "$PY" -m hub.auth bootstrap "$admin" >/dev/null)
        echo "admin: $admin"
    fi
    systemctl --user restart pcg-hub.service
    sleep 3
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8480/)
    [ "$code" = 401 ] || die "the hub answered $code without an Access login (expected 401)"
    echo "hub: Cloudflare Access login (team $team) at $url"
    ;;
local)
    set_env "$E" PCG_AUTH local
    systemctl --user restart pcg-hub.service
    echo "hub: its own invite links again (PCG_PUBLIC_URL unchanged: $(grep '^PCG_PUBLIC_URL=' "$E" | cut -d= -f2-))"
    ;;
status)
    grep -E '^PCG_(AUTH|PUBLIC_URL|CF_TEAM|ADMIN_EMAILS)=' "$E" || true
    for u in pcg-hub pcg-voice pcg-tunnel; do printf '%-11s %s\n' "$u" "$(systemctl --user is-active $u 2>/dev/null || true)"; done
    ;;
*) die "usage: cloudflare.sh tunnel | login --team T --aud A --url URL [--admin EMAIL] | local | status" ;;
esac
