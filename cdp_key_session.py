"""Scoped CDP response handling for the TV-local key-command endpoint.

Never log protocol events: requests can include temporary authorization headers.
"""
import json
import time

KEY_URL = 'http://127.0.0.1:7346/key_command/'


class KeySession:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.continuations = set()
        self.enabled = False

    def send(self, method, params):
        self.next_id += 1
        command_id = self.next_id
        self.ws.send(json.dumps({'id': command_id, 'method': method, 'params': params}))
        return command_id

    def continue_response(self, event):
        request_id = event['requestId']
        if event.get('request', {}).get('url') != KEY_URL or 'responseStatusCode' not in event:
            method = 'Fetch.continueRequest'
            params = {'requestId': request_id}
        else:
            headers = [h for h in event.get('responseHeaders', [])
                       if not h['name'].lower().startswith('access-control-')]
            headers.extend([
                {'name': 'Access-Control-Allow-Origin', 'value': '*'},
                {'name': 'Access-Control-Allow-Methods', 'value': 'PUT, OPTIONS'},
                {'name': 'Access-Control-Allow-Headers', 'value': 'AUTH, Content-Type'},
                {'name': 'Access-Control-Max-Age', 'value': '0'},
            ])
            method = 'Fetch.continueResponse'
            # Preserve the backend status. A 500 must never become a success.
            params = {'requestId': request_id, 'responseCode': event['responseStatusCode'],
                      'responseHeaders': headers}
        self.continuations.add(self.send(method, params))

    def call(self, method, params, timeout=10):
        command_id = self.send(method, params)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.ws.settimeout(max(.1, deadline - time.monotonic()))
            raw = self.ws.recv()
            if not raw:
                raise ConnectionError('Debugger closed')
            message = json.loads(raw)
            if message.get('method') == 'Fetch.requestPaused':
                self.continue_response(message['params'])
            elif message.get('id') in self.continuations:
                self.continuations.discard(message['id'])
                if message.get('error'):
                    raise ValueError('Scoped response handling failed')
            elif message.get('id') == command_id:
                if message.get('error'):
                    raise ValueError('Debugger command failed')
                return message.get('result', {})
        raise TimeoutError('Debugger response timeout')

    def enable(self):
        self.call('Fetch.enable', {'patterns': [{'urlPattern': KEY_URL, 'requestStage': 'Response'}]})
        self.enabled = True

    def evaluate(self, expression, timeout):
        result = self.call('Runtime.evaluate', {'expression': expression, 'awaitPromise': True,
                                               'returnByValue': True}, timeout)
        if result.get('exceptionDetails'):
            raise ValueError('Target evaluation failed')
        value = result.get('result', {}).get('value')
        if not isinstance(value, dict):
            raise ValueError('Unexpected debugger reply')
        return value

    def close(self):
        if self.enabled:
            try:
                self.call('Fetch.disable', {}, timeout=2)
            except Exception:
                pass
            self.enabled = False
