"""Small, dependency-free Vizio SmartCast remote. Python 3.9+."""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
INPUT = '/menu_native/dynamic/tv_settings/devices/current_input'
INPUTS = '/menu_native/dynamic/tv_settings/devices/name_input'
KEYS = {
    'power-off': (11, 0), 'power-on': (11, 1), 'power-toggle': (11, 2),
    'volume-down': (5, 0), 'volume-up': (5, 1), 'mute-off': (5, 2),
    'mute-on': (5, 3), 'mute-toggle': (5, 4), 'input-next': (7, 1),
    'up': (3, 8), 'down': (3, 0), 'left': (3, 1), 'right': (3, 7),
    'ok': (3, 2), 'back': (4, 0), 'home': (4, 15), 'menu': (4, 8),
    'play': (2, 3), 'pause': (2, 2),
}


class TVError(Exception):
    pass


def redact(value):
    if isinstance(value, dict):
        return {k: '[redacted]' if any(s in k.lower() for s in
                ('token', 'password', 'secret')) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


class TV:
    def __init__(self, config):
        self.config = config
        self.host = str(ipaddress.IPv4Address(config['host']))
        # This TV presents a vendor certificate for a DNS name, not its LAN IP.
        # Disable verification only in this client's dedicated HTTPS context.
        self.context = ssl._create_unverified_context()

    def request(self, path, body=None, paired=False, port=None):
        if not path.startswith('/') or path.startswith('//') or '://' in path:
            raise TVError('Use a relative API path starting with one slash.')
        headers = {'Content-Type': 'application/json'}
        if paired:
            token = self.config.get('auth_token')
            if not token:
                raise TVError('Pair first: pair-start, then pair-finish.')
            headers['AUTH'] = token
        port = port or self.config.get('port', 7345)
        scheme = 'http' if port == 8008 else 'https'
        req = urllib.request.Request(f'{scheme}://{self.host}:{port}{path}',
                data=None if body is None else json.dumps(body).encode(),
                headers=headers, method='GET' if body is None else 'PUT')
        handlers = [urllib.request.ProxyHandler({})]
        if scheme == 'https':
            handlers.append(urllib.request.HTTPSHandler(context=self.context))
        try:
            with urllib.request.build_opener(*handlers).open(req, timeout=6) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise TVError(f'HTTP {exc.code} for {path}; pairing may be required.') from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TVError(f'Request failed for {path}: {exc}') from None
        if isinstance(data, dict):
            result = data.get('STATUS', {}).get('RESULT')
            if result and result.upper() != 'SUCCESS':
                raise TVError(f'TV rejected {path}: {result}')
        return data

    def key(self, name):
        codeset, code = KEYS[name]
        return self.request('/key_command/', {'KEYLIST': [
            {'CODESET': codeset, 'CODE': code, 'ACTION': 'KEYPRESS'}]}, paired=True)

    def set_input(self, name):
        available = self.request(INPUTS, paired=True).get('ITEMS', [])
        valid = [i['NAME'] for i in available if i.get('ENABLED', True) and 'NAME' in i]
        if name not in valid:
            raise TVError('Choose an exact input name from: ' + ', '.join(valid))
        current = self.request(INPUT, paired=True)['ITEMS'][0]
        return self.request(INPUT, {'REQUEST': 'MODIFY', 'VALUE': name,
                                   'HASHVAL': current['HASHVAL']}, paired=True)

    def snapshot(self):
        paths = {'device': '/state/device/deviceinfo', 'power': '/state/device/power_mode',
                 'inputs': INPUTS, 'current_input': INPUT, 'current_app': '/app/current',
                 'audio': '/menu_native/dynamic/tv_settings/audio',
                 'system': '/menu_native/dynamic/tv_settings/system'}
        data = {}
        for label, path in paths.items():
            try:
                data[label] = self.request(path, paired=bool(self.config.get('auth_token')))
            except TVError as exc:
                data[label] = {'error': str(exc)}
        try:
            data['cast'] = self.request('/setup/eureka_info', port=8008)
        except TVError as exc:
            data['cast'] = {'error': str(exc)}
        return data


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=ROOT / '.local' / 'tv.json')
    p.add_argument('--host', help='IPv4 address; saved when starting pairing')
    p.add_argument('--output', type=Path, help='Save redacted JSON result to this file')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('pair-start')
    sub.add_parser('pair-finish')
    sub.add_parser('pair-cancel')
    for name in ('info', 'status', 'inputs', 'current-input', 'current-app', 'snapshot'):
        sub.add_parser(name)
    power = sub.add_parser('power')
    power.add_argument('action', choices=['on', 'off', 'toggle'])
    key = sub.add_parser('key')
    key.add_argument('name', choices=sorted(KEYS))
    inp = sub.add_parser('input')
    inp.add_argument('name', help='Exact NAME from inputs, such as HDMI-1')
    get = sub.add_parser('get', help='Read a relative SmartCast API path')
    get.add_argument('path')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = json.loads(args.config.read_text()) if args.config.exists() else {
            'host': '192.168.50.233', 'port': 7345,
            'device_id': str(uuid.uuid4()), 'device_name': 'Joe Python Remote'}
        if args.host:
            if args.host != config['host']:
                config.pop('auth_token', None)
                config.pop('pairing', None)
            config['host'] = args.host
        tv = TV(config)
        cmd = args.command
        if cmd == 'pair-start':
            data = tv.request('/pairing/start', {'DEVICE_ID': config['device_id'],
                                               'DEVICE_NAME': config['device_name']})
            item = data.get('ITEM', {})
            if 'PAIRING_REQ_TOKEN' not in item or 'CHALLENGE_TYPE' not in item:
                raise TVError('TV did not return a pairing challenge.')
            config['pairing'] = item
            save(args.config, config)
            data = {'message': 'Code requested. Read it on the TV, then run pair-finish.'}
        elif cmd in ('pair-finish', 'pair-cancel'):
            challenge = config.get('pairing')
            if not challenge:
                raise TVError('Run pair-start first.')
            code = getpass.getpass('Code displayed on TV: ') if cmd == 'pair-finish' else '1111'
            if cmd == 'pair-finish' and not (len(code) == 4 and code.isascii() and code.isdigit()):
                raise TVError('Expected a four-digit code.')
            endpoint = '/pairing/pair' if cmd == 'pair-finish' else '/pairing/cancel'
            data = tv.request(endpoint, {'DEVICE_ID': config['device_id'],
                'CHALLENGE_TYPE': challenge['CHALLENGE_TYPE'],
                'PAIRING_REQ_TOKEN': challenge['PAIRING_REQ_TOKEN'], 'RESPONSE_VALUE': code})
            if cmd == 'pair-finish':
                token = data.get('ITEM', {}).get('AUTH_TOKEN')
                if not token:
                    raise TVError('No authentication token returned.')
                config['auth_token'] = token
            config.pop('pairing', None)
            save(args.config, config)
            data = {'message': 'Paired successfully.' if cmd == 'pair-finish' else 'Pairing cancelled.'}
        elif cmd == 'power':
            data = tv.key('power-' + args.action)
        elif cmd == 'key':
            data = tv.key(args.name)
        elif cmd == 'input':
            data = tv.set_input(args.name)
        elif cmd == 'snapshot':
            data = tv.snapshot()
        else:
            paths = {'info': '/state/device/deviceinfo', 'status': '/state/device/power_mode',
                     'inputs': INPUTS, 'current-input': INPUT, 'current-app': '/app/current'}
            data = tv.request(args.path if cmd == 'get' else paths[cmd],
                              paired=bool(config.get('auth_token')))
        data = redact(data)
        if args.output:
            save(args.output, data)
        print(json.dumps(data, indent=2))
        return 0
    except (TVError, ValueError, KeyError, OSError) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
