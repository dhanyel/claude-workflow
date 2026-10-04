#!/usr/bin/env python3
"""Local proxy between opencode and z.ai (spec §5.2).

Why it exists: Bun (inside opencode) reuses keep-alive connections; z.ai closes an idle connection after 30-60 s.
Turn 1 works, turn 2 hangs on the dead connection (anomalyco/opencode#15350, closed as "not planned"). This proxy
opens a NEW upstream connection for every request (`Connection: close`) and relays the stream byte by byte, so Bun's
pool never reuses anything broken.

Usage:  zai_proxy.py [port]        (default 8788)
        opencode baseURL -> http://127.0.0.1:8788
        ZAI_UPSTREAM overrides the upstream (the tests point it at a local server).

⚠️ It never logs a request body or a header: the body is the plan under review and the Authorization header is the
user's key. Only the request line goes to stderr.
"""
import http.server, os, socketserver, sys, urllib.error, urllib.request

UPSTREAM = os.environ.get("ZAI_UPSTREAM", "https://api.z.ai/api/coding/paas/v4")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8788


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("[zai-proxy] " + (fmt % a) + "\n")

    def _relay(self, method):
        body = None
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)

        req = urllib.request.Request(UPSTREAM.rstrip("/") + self.path, data=body, method=method)
        for h in ("authorization", "content-type", "accept"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        # the line that fixes it: never reuse an upstream connection
        req.add_header("Connection", "close")

        try:
            r = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except Exception as e:
            msg = ('{"error":{"message":"proxy: %s"}}' % str(e).replace('"', "'")).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return

        ct = r.headers.get("Content-Type", "application/json")
        stream = "event-stream" in ct or "chunked" in (r.headers.get("Transfer-Encoding") or "")
        self.send_response(r.status)
        self.send_header("Content-Type", ct)
        if stream:
            # SSE: no Content-Length; close at the end so the client knows it is over
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = r.read(1)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
        else:
            data = r.read()
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

    def do_POST(self):
        self._relay("POST")

    def do_GET(self):
        self._relay("GET")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(("127.0.0.1", PORT), Handler) as s:
        sys.stderr.write(f"[zai-proxy] 127.0.0.1:{PORT} -> {UPSTREAM}\n")
        s.serve_forever()
