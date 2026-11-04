"""PIN-paired SmartCast remote support; never request a PIN without confirmation."""
import getpass
import json
from pathlib import Path
import uuid

from vizio import TV, TVError, save

ROOT = Path(__file__).resolve().parent


class PairingDeclined(TVError):
    pass


def load_config(host, path=None, api_port=None):
    destination = path or ROOT / '.local' / f'remote-{host}.json'
    source = destination
    if path is None and not destination.exists():
        candidate = ROOT / '.local' / 'tv.json'
        if candidate.exists():
            existing = json.loads(candidate.read_text(encoding='utf-8'))
            if existing.get('host') == host and existing.get('auth_token'):
                source = candidate
    if source.exists():
        config = json.loads(source.read_text(encoding='utf-8'))
        if not isinstance(config, dict) or config.get('host') != host:
            raise TVError('Pairing configuration belongs to a different TV; select its matching host or another --config.')
    else:
        config = {'host': host, 'port': 7345, 'device_id': str(uuid.uuid4()),
                  'device_name': 'VizioControl Python Remote'}
    config.setdefault('device_id', str(uuid.uuid4()))
    config.setdefault('device_name', 'VizioControl Python Remote')
    config.setdefault('port', 7345)
    if api_port is not None:
        config['port'] = api_port
    if type(config['port']) is not int or not 1 <= config['port'] <= 65535:
        raise TVError('Pairing configuration requires a valid integer API port.')
    return destination, config


def pair_if_needed(path, config, force=False, input_fn=None, code_fn=None):
    if config.get('auth_token') and not force:
        return TV(config)
    read = input_fn or input
    try:
        answer = read(f"TV {config['host']}: request a pairing code on the TV now? [y/N] ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        raise PairingDeclined('Pairing cancelled; no pairing request sent.') from None
    if answer not in ('y', 'yes'):
        raise PairingDeclined('Pairing declined; no pairing request or buttons sent.')
    tv = TV(config)
    old = config.get('pairing')
    if old:
        tv.request('/pairing/cancel', {'DEVICE_ID': config['device_id'],
                   'CHALLENGE_TYPE': old['CHALLENGE_TYPE'],
                   'PAIRING_REQ_TOKEN': old['PAIRING_REQ_TOKEN'], 'RESPONSE_VALUE': '1111'})
        config.pop('pairing', None)
        save(path, config)
    response = tv.request('/pairing/start', {'DEVICE_ID': config['device_id'],
                                           'DEVICE_NAME': config['device_name']})
    item = response.get('ITEM', {})
    if 'PAIRING_REQ_TOKEN' not in item or 'CHALLENGE_TYPE' not in item:
        raise TVError('TV did not return a pairing challenge.')
    config['pairing'] = {key: item[key] for key in ('PAIRING_REQ_TOKEN', 'CHALLENGE_TYPE')}
    save(path, config)
    print('Pairing code requested. Read the four-digit code on the TV; input is hidden.')
    read_code = code_fn or getpass.getpass
    try:
        while True:
            code = read_code('TV code (q to cancel): ').strip()
            if code.casefold() == 'q':
                raise PairingDeclined('Pairing cancelled; no buttons sent.')
            if len(code) == 4 and code.isascii() and code.isdigit():
                break
            print('Enter the four-digit TV code, or q.')
    except (EOFError, KeyboardInterrupt, PairingDeclined):
        tv.request('/pairing/cancel', {'DEVICE_ID': config['device_id'],
                   'CHALLENGE_TYPE': item['CHALLENGE_TYPE'],
                   'PAIRING_REQ_TOKEN': item['PAIRING_REQ_TOKEN'], 'RESPONSE_VALUE': '1111'})
        config.pop('pairing', None)
        save(path, config)
        raise PairingDeclined('Pairing cancelled; no buttons sent.') from None
    response = tv.request('/pairing/pair', {'DEVICE_ID': config['device_id'],
                          'CHALLENGE_TYPE': item['CHALLENGE_TYPE'],
                          'PAIRING_REQ_TOKEN': item['PAIRING_REQ_TOKEN'], 'RESPONSE_VALUE': code})
    code = None
    token = response.get('ITEM', {}).get('AUTH_TOKEN')
    if not isinstance(token, str) or not token:
        raise TVError('No pairing credential returned.')
    config['auth_token'] = token
    config.pop('pairing', None)
    save(path, config)
    print('Paired successfully. Credential saved locally; not displayed.')
    return TV(config)


def send_keys(tv, keys):
    try:
        tv.request('/key_command/', {'KEYLIST': keys}, paired=True)
        return {'ok': True, 'status': 'accepted', 'count': len(keys), 'transport': 'paired'}
    except TVError as exc:
        # No raw response, token, challenge, or request header is printed.
        if str(exc).startswith(('Request failed', 'HTTP 5')):
            return {'ok': False, 'status': 'request_interrupted', 'outcome': 'unknown', 'transport': 'paired'}
        return {'ok': False, 'status': 'rejected', 'transport': 'paired'}
