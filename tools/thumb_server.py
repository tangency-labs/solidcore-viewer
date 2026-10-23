#!/usr/bin/env python3
"""Serve <sitedir> on 127.0.0.1:<port> and save thumbnails POSTed by thumbs.html to
<sitedir>/thumb/<id>.webp. usage: thumb_server.py <sitedir> <port>"""
import http.server, os, re, sys

site, port = sys.argv[1], int(sys.argv[2])
os.makedirs(os.path.join(site, "thumb"), exist_ok=True)


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=site, **k)

    def do_POST(self):
        m = re.fullmatch(r"/thumb/([A-Za-z0-9_\-]+)\.webp", self.path)
        n = int(self.headers.get("Content-Length", 0))
        if not m or n <= 0 or n > 2_000_000:
            self.send_response(400); self.end_headers(); return
        data = self.rfile.read(n)
        with open(os.path.join(site, "thumb", m.group(1) + ".webp"), "wb") as fh:
            fh.write(data)
        self.send_response(204); self.end_headers()

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
