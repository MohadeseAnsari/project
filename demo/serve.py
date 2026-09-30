"""Static file server for the demo UI.

Exists because ``python -m http.server`` gets the Content-Type wrong for the
webfonts: the slim Python image ships no ``/etc/mime.types``, so ``.woff2`` is
unrecognised and every font is served as ``application/octet-stream``. Browsers
refuse to load a font served with the wrong MIME type, so Vazirmatn silently
fell back to the system font with no error in the console to explain it.

Threaded so one slow request cannot block the page and its assets, and marked
no-store so a rebuilt demo is picked up on reload instead of being served from
the browser cache.
"""

import functools
import http.server
import mimetypes
import socketserver

# The types this demo actually serves. woff2 is the important one.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static handler with correct font types and no caching."""

    # HTTP/1.1 with keep-alive. The default is HTTP/1.0, which closes the
    # connection after every response. Firefox opens six parallel requests for
    # this page (HTML, CSS, JS, four webfonts) and pools them on one connection,
    # so with HTTP/1.0 the server answered the first and dropped the other five;
    # Firefox then stalled roughly 20s re-establishing them, and the page sat
    # inert the whole time -- no click handlers attached, health check never
    # run. SimpleHTTPRequestHandler always sends Content-Length, so HTTP/1.1
    # keep-alive is safe here.
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        # One line per asset; the default format is noisy and unparseable here.
        print(f"{self.address_string()} - {fmt % args}", flush=True)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    port = 8080
    handler = functools.partial(Handler, directory="/srv/demo")
    with Server(("0.0.0.0", port), handler) as httpd:
        print(f"Demo UI on :{port}", flush=True)
        httpd.serve_forever()
