"""Expose exactly one selected media file to a TV with HTTP byte-range support."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import socket
import threading
from urllib.parse import urlsplit
import uuid


def byte_range(value, size):
    if value is None:
        return 0, size - 1, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', value.strip())
    if not match or not any(match.groups()):
        raise ValueError('Unsupported range')
    start, end = match.groups()
    if not start:
        length = int(end)
        if length <= 0:
            raise ValueError('Invalid suffix range')
        return max(0, size - length), size - 1, True
    start = int(start)
    end = min(int(end), size - 1) if end else size - 1
    if start >= size or start > end:
        raise ValueError('Unsatisfiable range')
    return start, end, True


class QuietMediaHTTPServer(ThreadingHTTPServer):
    def handle_error(self, _request, _client_address):
        # Client disconnects must not dump paths, addresses, or request data.
        pass


class MediaFileServer:
    def __init__(self, path, tv_host, content_type, bind=None):
        self.path = Path(path).resolve(strict=True)
        if not self.path.is_file() or self.path.stat().st_size == 0:
            raise ValueError('Choose a nonempty media file')
        self.content_type = content_type
        self.request_count = 0
        self.bytes_sent = 0
        self.route = '/media/' + uuid.uuid4().hex + '/video'
        if bind is None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((tv_host, 8009))
                bind = route.getsockname()[0]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def do_OPTIONS(self):
                self.respond(headers_only=True, options=True)

            def do_HEAD(self):
                self.respond(headers_only=True)

            def do_GET(self):
                self.respond()

            def respond(self, headers_only=False, options=False):
                # No directory serving, URL-to-filesystem mapping, or arbitrary file reads.
                if urlsplit(self.path).path != outer.route:
                    self.send_error(404)
                    return
                outer.request_count += 1
                size = outer.path.stat().st_size
                try:
                    start, end, partial = byte_range(self.headers.get('Range'), size)
                except ValueError:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self.send_response(204 if options else 206 if partial else 200)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Range')
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Type', outer.content_type)
                self.send_header('Content-Length', str(0 if options else end - start + 1))
                if partial and not options:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                if headers_only:
                    return
                remaining = end - start + 1
                try:
                    with outer.path.open('rb') as source:
                        source.seek(start)
                        while remaining:
                            block = source.read(min(65536, remaining))
                            if not block:
                                return
                            self.wfile.write(block)
                            remaining -= len(block)
                            outer.bytes_sent += len(block)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = QuietMediaHTTPServer((bind, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f'http://{bind}:{self.server.server_port}{self.route}'

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
