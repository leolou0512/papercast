"""A small SMTP server for the tests (smtpd is gone from newer Pythons): socketserver, one thread
per connection, the commands smtplib sends. STARTTLS (and TLS from the start, as on port 465)
with a self-signed certificate for 127.0.0.1 made by `openssl`, AUTH PLAIN, and a list of what
arrived. It refuses AUTH before TLS, as a real submission server does.

    s = FakeSMTP(tmpdir, user="bot@example.org", password="app-pass").start()
    ...  cfg.smtp_host = "127.0.0.1"; cfg.smtp_port = s.port; cfg.smtp_ca_file = s.ca
    s.messages -> [{"from", "to": [...], "data": bytes, "user", "tls"}]
    s.fail = "auth" | "data" | None
"""
from __future__ import annotations

import base64
import email
import email.policy
import shutil
import socketserver
import ssl
import subprocess
import threading
from pathlib import Path

OPENSSL = shutil.which("openssl")


def self_signed(d: Path) -> tuple:
    crt, key = d / "smtp.crt", d / "smtp.key"
    if not crt.exists():
        subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                        "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                       check=True, capture_output=True)
    return str(crt), str(key)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeSMTP:
    def __init__(self, d: Path, *, user: str | None = None, password: str | None = None, implicit_tls: bool = False,
                 starttls: bool = True):
        self.ca, key = self_signed(Path(d))
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(self.ca, key)
        self.user, self.password = user, password
        self.implicit_tls, self.starttls = implicit_tls, starttls
        self.messages: list = []
        self.fail = None
        self.lock = threading.Lock()
        self.arrived = threading.Condition(self.lock)
        self._open: dict = {}                   # thread -> the TLS sockets it made (closed after)
        outer = self

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    outer._session(self.request)
                except (OSError, ssl.SSLError, ValueError):
                    pass
                finally:
                    for s in outer._open.pop(threading.get_ident(), []):
                        try:
                            s.close()
                        except OSError:
                            pass

        self.srv = _Server(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]

    def start(self):
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def wait(self, n: int, timeout: float = 10.0) -> bool:
        with self.arrived:
            return self.arrived.wait_for(lambda: len(self.messages) >= n, timeout)

    def parsed(self, i: int = -1):
        return email.message_from_bytes(self.messages[i]["data"], policy=email.policy.default)

    def _session(self, sock):
        tls = False
        mine = self._open.setdefault(threading.get_ident(), [])
        if self.implicit_tls:
            sock = self.ctx.wrap_socket(sock, server_side=True)
            mine.append(sock)
            tls = True
        f = sock.makefile("rb")

        def say(line: str):
            sock.sendall(line.encode() + b"\r\n")

        say("220 fake-smtp ESMTP ready")
        who, mail_from, rcpts = None, None, []
        while True:
            raw = f.readline(4096)
            if not raw:
                return
            cmd = raw.decode("utf-8", "replace").rstrip("\r\n")
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                caps = ["fake-smtp", "8BITMIME", "SIZE 10485760"]
                if not tls and self.starttls:
                    caps.append("STARTTLS")
                if tls:
                    caps.append("AUTH PLAIN")
                for i, c in enumerate(caps):
                    say(("250 " if i == len(caps) - 1 else "250-") + c)
            elif up == "STARTTLS" and not tls and self.starttls:
                say("220 go ahead")
                sock = self.ctx.wrap_socket(sock, server_side=True)
                mine.append(sock)
                f = sock.makefile("rb")
                tls, who, mail_from, rcpts = True, None, None, []
            elif up.startswith("AUTH PLAIN"):
                if not tls:
                    say("530 5.7.0 must issue a STARTTLS command first")
                    continue
                parts = cmd.split()
                if len(parts) == 3:
                    b64 = parts[2]
                else:
                    say("334 ")
                    b64 = f.readline(4096).decode().strip()
                try:
                    _, u, p = base64.b64decode(b64).decode().split("\0")
                except ValueError:
                    say("501 bad auth")
                    continue
                if self.fail == "auth" or (u, p) != (self.user, self.password):
                    say("535 5.7.8 Username and Password not accepted")
                    continue
                who = u
                say("235 2.7.0 Accepted")
            elif up.startswith("MAIL FROM:"):
                if self.user and who is None:
                    say("530 5.7.0 Authentication Required")
                    continue
                mail_from, rcpts = cmd[10:].strip().strip("<>").split(">")[0], []
                say("250 OK")
            elif up.startswith("RCPT TO:"):
                rcpts.append(cmd[8:].strip().strip("<>").split(">")[0])
                say("250 OK")
            elif up == "DATA":
                if not mail_from or not rcpts:
                    say("503 need MAIL and RCPT")
                    continue
                say("354 end with <CRLF>.<CRLF>")
                lines = []
                while True:
                    ln = f.readline(1 << 20)
                    if not ln or ln == b".\r\n":
                        break
                    lines.append(ln[1:] if ln.startswith(b"..") else ln)
                if self.fail == "data":
                    say("451 4.3.0 try again later")
                    continue
                with self.arrived:
                    self.messages.append({"from": mail_from, "to": list(rcpts), "data": b"".join(lines), "user": who, "tls": tls})
                    self.arrived.notify_all()
                say("250 OK queued")
                mail_from, rcpts = None, []
            elif up in ("RSET", "NOOP"):
                mail_from, rcpts = (None, []) if up == "RSET" else (mail_from, rcpts)
                say("250 OK")
            elif up == "QUIT":
                say("221 bye")
                return
            else:
                say("502 not implemented")
