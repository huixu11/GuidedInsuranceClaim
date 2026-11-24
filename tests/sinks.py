"""Local receivers for the delivery tests: a webhook endpoint and an SMTP server on 127.0.0.1,
each recording what it receives and answering from a script (then: success)."""

from __future__ import annotations

import email
import email.policy
import json
import socketserver
import threading
import time
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class WebhookSink:
    """POSTs are recorded ({path, headers (lower-case names), json}) and answered with the next
    scripted action: a status (500), a status with a Retry-After ("429:30"), or "hang" (no
    answer for `hang_seconds`). 200 once the script is used up."""

    def __init__(self, script: Iterable[Any] = (), hang_seconds: float = 2.0) -> None:
        self.script: list[Any] = list(script)
        self.hang_seconds = hang_seconds
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server name
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with sink.lock:
                    sink.requests.append(
                        {
                            "path": self.path,
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                            "json": json.loads(body or b"null"),
                        }
                    )
                    action = sink.script.pop(0) if sink.script else 200
                if action == "hang":
                    time.sleep(sink.hang_seconds)
                    return
                status, _, retry_after = str(action).partition(":")
                self.send_response(int(status))
                if retry_after:
                    self.send_header("Retry-After", retry_after)
                if 300 <= int(status) < 400:
                    self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hooks/handoff?team=claims"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def keys(self) -> list[str]:
        with self.lock:
            return [r["headers"].get("idempotency-key", "") for r in self.requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class SmtpSink:
    """A minimal SMTP server (no TLS, no AUTH). The end of each DATA is answered with the next
    scripted reply ("451 4.3.0 Try again later"); "250 OK" once the script is used up. Accepted
    messages are recorded as {from, to, raw, message}."""

    def __init__(self, script: Iterable[str] = ()) -> None:
        self.script = list(script)
        self.messages: list[dict[str, Any]] = []
        self.attempts = 0  # DATA commands received
        self.lock = threading.Lock()
        sink = self

        class Handler(socketserver.StreamRequestHandler):
            timeout = 10

            def say(self, line: str) -> None:
                self.wfile.write(line.encode("ascii") + b"\r\n")

            def handle(self) -> None:
                self.say("220 sink.test ESMTP")
                sender, recipients = "", []
                while line := self.rfile.readline().decode("utf-8", "replace").rstrip("\r\n"):
                    verb, _, rest = line.partition(" ")
                    verb = verb.upper()
                    address = rest.partition(":")[2].strip().split(" ")[0].strip("<>")
                    if verb in ("EHLO", "HELO"):
                        self.wfile.write(b"250-sink.test\r\n250 8BITMIME\r\n")
                    elif verb == "MAIL":
                        sender, recipients = address, []
                        self.say("250 OK")
                    elif verb == "RCPT":
                        recipients.append(address)
                        self.say("250 OK")
                    elif verb == "DATA":
                        self.say("354 End data with <CR><LF>.<CR><LF>")
                        lines = []
                        while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                            lines.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                        raw = b"".join(lines)
                        with sink.lock:
                            sink.attempts += 1
                            answer = sink.script.pop(0) if sink.script else "250 OK"
                            if answer.startswith("250"):
                                message = email.message_from_bytes(raw, policy=email.policy.default)
                                sink.messages.append({"from": sender, "to": list(recipients), "raw": raw,
                                                      "message": message})
                        self.say(answer)
                    elif verb in ("RSET", "NOOP"):
                        self.say("250 OK")
                    elif verb == "QUIT":
                        self.say("221 Bye")
                        return
                    else:
                        self.say("502 Command not implemented")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"smtp://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
