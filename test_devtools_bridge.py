"""Exercise the bridge against a local simulated debugger; no TV required."""
import base64
import hashlib
import http.client
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from devtools_bridge import handler_for

PATH = '/devtools/page/test-page'


class FakeDebugger(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.origins.append(self.headers.get('Origin'))
        self.server.paths.append(self.path)
        key = self.headers['Sec-WebSocket-Key']
        accept = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101)
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', accept)
        self.end_headers()
        self.close_connection = True
        data = self.connection.recv(4096)
        if data:
            self.connection.sendall(data)


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), FakeDebugger)
        cls.upstream.origins = []
        cls.upstream.paths = []
        cls.upstream.daemon_threads = True
        cls.targets = [{'type': 'page', 'title': 'Test page',
                        'webSocketDebuggerUrl': f'ws://127.0.0.1:{cls.upstream.server_port}{PATH}'}]
        cls.bridge = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(
            '127.0.0.1', cls.upstream.server_port, cls.targets, {}, {PATH}))
        cls.bridge.daemon_threads = True
        for server in [cls.upstream, cls.bridge]:
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in [cls.bridge, cls.upstream]:
            server.shutdown()
            server.server_close()

    def request(self, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.bridge.server_port, timeout=3)
        try:
            conn.request('GET', path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_metadata_rewrites_to_loopback_without_mutating_source(self):
        status, body = self.request('/json/list')
        self.assertEqual(status, 200)
        item = json.loads(body)[0]
        self.assertEqual(item['webSocketDebuggerUrl'], f'ws://127.0.0.1:{self.bridge.server_port}{PATH}')
        self.assertTrue(item['devtoolsFrontendUrl'].startswith('devtools://'))
        self.assertNotIn('devtoolsFrontendUrl', self.targets[0])

    def test_untrusted_origin_is_rejected(self):
        status, _ = self.request(PATH, {'Origin': 'https://untrusted.invalid', 'Upgrade': 'websocket'})
        self.assertEqual(status, 403)

    def test_unexpected_host_is_rejected(self):
        status, _ = self.request('/json/list', {'Host': 'untrusted.invalid'})
        self.assertEqual(status, 403)

    def test_unadvertised_target_is_rejected(self):
        status, _ = self.request('/devtools/page/other', {'Upgrade': 'websocket'})
        self.assertEqual(status, 404)

    def test_websocket_origin_removed_and_bytes_relayed(self):
        with socket.create_connection(('127.0.0.1', self.bridge.server_port), timeout=3) as sock:
            request = (f'GET {PATH} HTTP/1.1\r\nHost: 127.0.0.1:{self.bridge.server_port}\r\n'
                       'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                       'Origin: https://chrome-devtools-frontend.appspot.com\r\n'
                       'Sec-WebSocket-Version: 13\r\n'
                       'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n')
            sock.sendall(request.encode())
            headers = b''
            while b'\r\n\r\n' not in headers:
                block = sock.recv(4096)
                self.assertTrue(block)
                headers += block
            self.assertIn(b'101 Switching Protocols', headers)
            # A masked client text frame; the bridge must preserve all frame bytes.
            frame = b'\x81\x82\x01\x02\x03\x04ik'
            sock.sendall(frame)
            result = b''
            while len(result) < len(frame):
                block = sock.recv(4096)
                self.assertTrue(block)
                result += block
            self.assertEqual(result, frame)
        self.assertEqual(self.upstream.origins[-1], None)
        self.assertEqual(self.upstream.paths[-1], PATH)


if __name__ == '__main__':
    unittest.main()
