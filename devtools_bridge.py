"""Loopback-only bridge for Vizio CDP endpoints that reject browser Origins.

Uses the standard library. No TV settings are changed. Ctrl+C stops the bridge.
"""
import argparse
import ipaddress
import json
import select
import socket
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rediscover_devtools import http_get

TRUSTED_ORIGINS = {'devtools://devtools', 'chrome-devtools://devtools',
                   'https://chrome-devtools-frontend.appspot.com'}


def discover(host, port):
    _, body = http_get(host, port, '/json/list', timeout=4)
    targets = json.loads(body)
    _, body = http_get(host, port, '/json/version', timeout=4)
    version = json.loads(body)
    allowed = set()
    for item in targets + [version]:
        url = item.get('webSocketDebuggerUrl')
        if not url:
            continue
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'ws' or parsed.hostname != host or parsed.port != port
                or not parsed.path.startswith('/devtools/')):
            raise ValueError('Unexpected debugger address in target metadata')
        allowed.add(parsed.path)
    return targets, version, allowed


def handler_for(host, port, targets, version, allowed):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            # Do not log page URLs, request contents, or CDP messages.
            pass

        def do_GET(self):
            local = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') not in {local, f'localhost:{self.server.server_port}'}:
                self.send_error(403, 'Loopback Host required')
                return
            origin = self.headers.get('Origin')
            if origin and origin not in TRUSTED_ORIGINS | {f'http://{local}', f'http://localhost:{self.server.server_port}'}:
                self.send_error(403, 'Unexpected browser origin')
                return
            path = urllib.parse.urlsplit(self.path).path
            if self.headers.get('Upgrade', '').lower() == 'websocket':
                if path not in allowed:
                    self.send_error(404, 'Unknown debugger target; restart bridge after app changes')
                    return
                self.bridge(path)
                return
            if path in {'/json', '/json/list', '/json/version'}:
                data = version if path == '/json/version' else targets
                data = json.loads(json.dumps(data))
                for item in ([data] if isinstance(data, dict) else data):
                    if item.get('webSocketDebuggerUrl'):
                        ws_path = urllib.parse.urlsplit(item['webSocketDebuggerUrl']).path
                        item['webSocketDebuggerUrl'] = f'ws://{local}{ws_path}'
                        item['devtoolsFrontendUrl'] = f'devtools://devtools/bundled/inspector.html?ws={local}{ws_path}'
                self.respond(json.dumps(data).encode(), 'application/json')
            elif path == '/':
                self.respond(b'Vizio DevTools loopback bridge is running. Use chrome://inspect/#devices and configure this localhost port.', 'text/plain')
            else:
                self.send_error(404)

        def respond(self, body, mime):
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def bridge(self, path):
            self.close_connection = True
            try:
                with socket.create_connection((host, port), timeout=5) as upstream:
                    # Reuse the browser handshake, omitting Origin only on the TV leg.
                    lines = [f'GET {path} HTTP/1.1', f'Host: {host}:{port}']
                    for name in ('Upgrade', 'Connection', 'Sec-WebSocket-Key',
                                 'Sec-WebSocket-Version', 'Sec-WebSocket-Protocol'):
                        value = self.headers.get(name)
                        if value:
                            lines.append(f'{name}: {value}')
                    upstream.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii'))
                    response = b''
                    while b'\r\n\r\n' not in response:
                        piece = upstream.recv(4096)
                        if not piece:
                            raise OSError('TV closed handshake')
                        response += piece
                        if len(response) > 65536:
                            raise OSError('Oversized handshake')
                    self.connection.sendall(response)
                    if response.split(b'\r\n', 1)[0].split()[1] != b'101':
                        print('TV rejected debugger handshake', flush=True)
                        return
                    print('Debugger WebSocket connected through loopback bridge', flush=True)
                    upstream.settimeout(None)
                    self.connection.settimeout(None)
                    while True:
                        ready, _, _ = select.select([upstream, self.connection], [], [], 1)
                        for source in ready:
                            block = source.recv(65536)
                            if not block:
                                return
                            destination = self.connection if source is upstream else upstream
                            destination.sendall(block)
            except (OSError, ValueError):
                print('Debugger connection ended', flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, type=ipaddress.IPv4Address)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--listen', type=int, default=0, help='Loopback port; default picks an available port')
    parser.add_argument('--open', action='store_true', help='Open a working hosted DevTools frontend in Chrome')
    args = parser.parse_args()
    if not args.host.is_private or not 1 <= args.port <= 65535 or not 0 <= args.listen <= 65535:
        parser.error('Use a private TV address and valid ports')
    host = str(args.host)
    targets, version, allowed = discover(host, args.port)
    pages = [t for t in targets if t.get('type') == 'page' and t.get('webSocketDebuggerUrl')]
    if not pages:
        parser.error('No inspectable page found')
    server = ThreadingHTTPServer(('127.0.0.1', args.listen), handler_for(host, args.port, targets, version, allowed))
    server.daemon_threads = True
    ws_path = urllib.parse.urlsplit(pages[0]['webSocketDebuggerUrl']).path
    url = f'devtools://devtools/bundled/inspector.html?ws=127.0.0.1:{server.server_port}{ws_path}'
    print(f'Bridge: http://127.0.0.1:{server.server_port}/', flush=True)
    frontend = 'https://chrome-devtools-frontend.appspot.com/serve_rev/@c4b62201796ab08f26c2c13e4133c8f9ff54c438/inspector.html?' + url.split('?', 1)[1]
    print(f'Chrome DevTools: {url}', flush=True)
    print(f'Web frontend: {frontend}', flush=True)
    if args.open:
        chrome = next((p for p in [Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe'), Path(r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'), Path.home() / r'AppData\Local\Google\Chrome\Application\chrome.exe'] if p.exists()), None)
        if chrome:
            subprocess.Popen([str(chrome), '--new-window', frontend], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            print('Chrome executable not found. Open the Web frontend URL in Chrome.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
