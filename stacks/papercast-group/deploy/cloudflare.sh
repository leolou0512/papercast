#!/usr/bin/env bash
# The way in from the internet, on perov, and how the hub signs people in. Cloudflare only
# carries the traffic: a named tunnel from papercast.virtualatoms.org to http://127.0.0.1:8480,
# which the domain owner makes in his dashboard (README.md "Production"); he sends its token.
# Sign-in is the hub's own: passwords, with the group's list of allowed Imperial emails.
#
#   bash deploy/cloudflare.sh tunnel             start the tunnel (token in ~/papercast-group/tunnel.token, 600)
#   bash deploy/cloudflare.sh url --url https://papercast.virtualatoms.org
#                                                the hub's public address (links, Secure cookies); restarts the hub
#   bash deploy/cloudflare.sh auth password [--admin EMAIL]...
#                                                passwords and the list; the admins (by default yl6719@ic.ac.uk
#                                                and a.ganose@ic.ac.uk) go on it as admins; restarts the hub
#   bash deploy/cloudflare.sh auth local         back to the hub's own invite links (the tunnel stays up)
#   bash deploy/cloudflare.sh auth cf-access --team T --aud A [--admin EMAIL]   Cloudflare Access (not in use)
#   bash deploy/cloudflare.sh admins EMAIL...    put these on the list as admins (python3 -m hub.auth bootstrap)
#   bash deploy/cloudflare.sh email --host smtp.gmail.com --port 587 --user U --from U [--test ADDRESS]
#                                                the SMTP account for "forgot password" links; the password
#                                                goes in ~/papercast-group/smtp.password (600), never in hub.env
#   bash deploy/cloudflare.sh status
set -euo pipefail
. "$(dirname "$0")/lib.sh"
H=${PCG_HOME:-$HOME/papercast-group}
E=$H/hub.env
UNITS=$HOME/.config/systemd/user
PY=$H/venv/bin/python
APP=$H/app/papercast-group
ADMINS_DEFAULT="yl6719@ic.ac.uk a.ganose@ic.ac.uk"
USAGE="usage: cloudflare.sh tunnel | url --url URL | auth password|local|cf-access [...] | admins EMAIL... | email --host H --from F [--user U] [--port P] [--test ADDRESS] | status"

port() { local p; p=$(get_env "$E" PCG_PORT); echo "${p:-8480}"; }
code_of() { curl -s -m 15 -o /dev/null -w '%{http_code}' "$1" || true; }
hub() { (cd "$APP" && set -a && . "$E" && set +a && "$PY" -m hub.auth "$@"); }

# restart the hub and wait until it answers; prints the code of GET / (000: nothing answered)
restart_hub() {
    systemctl --user restart pcg-hub.service
    local c=000
    for _ in $(seq 1 20); do
        c=$(code_of "http://127.0.0.1:$(port)/")
        [ -n "$c" ] && [ "$c" != 000 ] && break
        sleep 1
    done
    echo "${c:-000}"
}

admins() {
    [ $# -gt 0 ] || die "admins needs at least one email"
    for a in "$@"; do
        hub bootstrap "$a" || die "could not make $a an admin (above)"
    done
}

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
url)
    url=
    while [ $# -gt 0 ]; do
        case "$1" in
        --url) url=${2%/}; shift 2 ;;
        *) die "unknown option $1" ;;
        esac
    done
    [ -n "$url" ] || die "url needs --url https://..."
    case "$url" in https://*) ;; *) die "--url must be https://... (the tunnel's public address)" ;; esac
    set_env "$E" PCG_PUBLIC_URL "$url"
    c=$(restart_hub)
    [ "$c" != 000 ] || die "the hub does not answer on 127.0.0.1:$(port) (journalctl --user -u pcg-hub)"
    echo "hub: public address $url (GET / -> $c)"
    ;;
auth)
    mode=${1:-}; shift || true
    case "$mode" in
    password)
        list=""
        while [ $# -gt 0 ]; do
            case "$1" in
            --admin) list="$list $2"; shift 2 ;;
            *) die "unknown option $1" ;;
            esac
        done
        set_env "$E" PCG_AUTH password
        # shellcheck disable=SC2086  (a list of emails)
        admins ${list:-$ADMINS_DEFAULT}
        case "$(get_env "$E" PCG_PUBLIC_URL)" in
        https://*) ;;
        *) echo "note: PCG_PUBLIC_URL is $(get_env "$E" PCG_PUBLIC_URL), not https: cookies are not Secure (cloudflare.sh url --url https://...)" ;;
        esac
        c=$(restart_hub)
        [ "$c" = 303 ] || die "the hub answered $c to a browser without a session (expected 303, to the sign-in page)"
        s=$(code_of "http://127.0.0.1:$(port)/signin")
        [ "$s" = 200 ] || die "the sign-in page answered $s"
        if [ -n "$(get_env "$E" PCG_SMTP_HOST)" ]; then
            echo "hub: passwords; \"forgot password\" links go out by email ($(get_env "$E" PCG_SMTP_HOST))"
        else
            echo "hub: passwords; no email set up yet (cloudflare.sh email ...): a forgotten password shows up for the admins"
        fi
        ;;
    local)
        set_env "$E" PCG_AUTH local
        restart_hub >/dev/null
        echo "hub: its own invite links again (PCG_PUBLIC_URL unchanged: $(get_env "$E" PCG_PUBLIC_URL))"
        ;;
    cf-access)
        team= aud= admin=
        while [ $# -gt 0 ]; do
            case "$1" in
            --team) team=${2%.cloudflareaccess.com}; shift 2 ;;
            --aud) aud=$2; shift 2 ;;
            --admin) admin=$2; shift 2 ;;
            *) die "unknown option $1" ;;
            esac
        done
        [ -n "$team" ] && [ -n "$aud" ] || die "auth cf-access needs --team and --aud"
        code=$(code_of "https://$team.cloudflareaccess.com/cdn-cgi/access/certs")
        [ "$code" = 200 ] || die "https://$team.cloudflareaccess.com/cdn-cgi/access/certs answered $code: is the team name right?"
        set_env "$E" PCG_AUTH cf-access
        set_env "$E" PCG_CF_TEAM "$team"
        set_env "$E" PCG_CF_AUD "$aud"
        if [ -n "$admin" ]; then
            cur=$(get_env "$E" PCG_ADMIN_EMAILS)
            case ",$cur," in *",$admin,"*) ;; *) set_env "$E" PCG_ADMIN_EMAILS "${cur:+$cur,}$admin" ;; esac
            hub bootstrap "$admin" >/dev/null
            echo "admin: $admin"
        fi
        c=$(restart_hub)
        [ "$c" = 303 ] || die "the hub answered $c without an Access login (expected 303, to the sign-in page)"
        echo "hub: Cloudflare Access login (team $team)"
        ;;
    *) die "auth password [--admin EMAIL]... | auth local | auth cf-access --team T --aud A [--admin EMAIL]" ;;
    esac
    ;;
admins)
    admins "$@"
    ;;
email)
    host= smtp_port=587 user= from= test=
    while [ $# -gt 0 ]; do
        case "$1" in
        --host) host=$2; shift 2 ;;
        --port) smtp_port=$2; shift 2 ;;
        --user) user=$2; shift 2 ;;
        --from) from=$2; shift 2 ;;
        --test) test=$2; shift 2 ;;
        *) die "unknown option $1" ;;
        esac
    done
    [ -n "$host" ] && [ -n "$from" ] || die "email needs --host and --from (and --user for a login)"
    case "$from" in *[[:space:]\<\>\"\'\$\`\;\&\|]*|*@*@*) die "--from is the bare address, like papercast.group@gmail.com (the emails say it is from papercast)" ;; *@*) ;; *) die "--from must be an email address" ;; esac
    case "$smtp_port" in 587|465) ;; *) die "--port is 587 (STARTTLS) or 465 (TLS from the start)" ;; esac
    pw=$H/smtp.password
    if [ -n "$user" ]; then
        [ -s "$pw" ] || die "no password at $pw: umask 077; cat > $pw (paste the app password, Enter, Ctrl-D)"
        chmod 600 "$pw"
        set_env "$E" PCG_SMTP_USER "$user"
        set_env "$E" PCG_SMTP_PASSWORD_FILE "$pw"
    fi
    set_env "$E" PCG_SMTP_HOST "$host"
    set_env "$E" PCG_SMTP_PORT "$smtp_port"
    set_env "$E" PCG_SMTP_FROM "$from"
    if [ -n "$test" ]; then
        hub email-test "$test" || die "the test email was not sent (above); the settings are saved"
    fi
    restart_hub >/dev/null
    echo "hub: \"forgot password\" links go out through $host:$smtp_port as ${user:-(no login)}"
    ;;
status)
    grep -E '^PCG_(AUTH|PUBLIC_URL|CF_TEAM|ADMIN_EMAILS|SMTP_HOST|SMTP_PORT|SMTP_USER|SMTP_FROM)=' "$E" || true
    for u in pcg-hub pcg-voice pcg-tunnel; do printf '%-11s %s\n' "$u" "$(systemctl --user is-active $u 2>/dev/null || true)"; done
    ;;
*) die "$USAGE" ;;
esac
