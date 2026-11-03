"""Replay Vizio discovery/status requests, then look for an existing DevTools listener.

Python 3.9+, standard library only. This does not enable a debug setting.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
BODY_LIMIT = 262144
CACHE = ROOT / '.local' / 'devtools-ports.json'
HISTORICAL_PORTS = [51013, 38713, 33012]


def log(message):
    print(message, flush=True)


def http_get(host, port, path, timeout=3, tls=False):
    """Only GET; bypass proxies and use a dedicated vendor-certificate context."""
    handlers = [urllib.request.ProxyHandler({})]
    if tls:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    url = f'{"https" if tls else "http"}://{host}:{port}{path}'
    request = urllib.request.Request(url, method='GET', headers={
        'User-Agent': 'VizioControl-local-inventory/1.0', 'Connection': 'close'})
    with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
        body = response.read(BODY_LIMIT + 1)
        if len(body) > BODY_LIMIT:
            raise ValueError('Response exceeds inventory size limit')
        return response.status, body


def ssdp(host, bind=None, duration=4):
    """Send standard local multicast discovery; retain only the requested TV's replies."""
    addresses = set()
    servers = set()
    count = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(.5)
        # Route selection avoids accidentally sending through a WSL/Hyper-V interface.
        if bind is None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect((host, 1900))
                bind = route.getsockname()[0]
        sock.bind((bind, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind))
        message = (b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
                   b'MAN: "ssdp:discover"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n')
        sock.sendto(message, ('239.255.255.250', 1900))
        end = time.monotonic() + duration
        while time.monotonic() < end:
            try:
                body, remote = sock.recvfrom(16384)
            except socket.timeout:
                continue
            if remote[0] != host:
                continue
            count += 1
            headers = {}
            for line in body.decode(errors='replace').splitlines()[1:]:
                if ':' in line:
                    key, value = line.split(':', 1)
                    headers[key.lower()] = value.strip()
            if headers.get('location'):
                addresses.add(headers['location'])
            if headers.get('server'):
                servers.add(headers['server'])
    return {'tv_replies': count, 'local_interface': bind,
            'advertised_locations': sorted(addresses), 'servers': sorted(servers)}


def summarize(name, body):
    if name == 'netflix':
        return {'response': body.decode(errors='replace')[:200]}
    if name == 'airplay':
        import plistlib
        data = plistlib.loads(body)
        return {key: data.get(key) for key in
                ('name', 'model', 'firmwareRevision', 'firmwareBuildDate', 'sourceVersion')}
    data = json.loads(body)
    result = data.get('STATUS', {}).get('RESULT')
    if result and result.upper() != 'SUCCESS':
        raise ValueError(f'TV returned {result}')
    if name == 'cast':
        return {key: data.get(key) for key in
                ('name', 'connected', 'uptime', 'cast_build_revision')}
    if name == 'device':
        value = data['ITEMS'][0]['VALUE']
        return {'model': value['MODEL_NAME'], 'name': value.get('CAST_NAME'),
                'firmware': value.get('SYSTEM_INFO', {}).get('VERSION')}
    return {'power_value': data['ITEMS'][0]['VALUE']}


def replay(host, bind=None):
    checks = []
    log('Replaying SSDP discovery, then Netflix, Cast, Vizio info/power and AirPlay GETs...')
    try:
        result = {'check': 'ssdp', **ssdp(host, bind)}
    except OSError as exc:
        result = {'check': 'ssdp', 'error': str(exc)}
    checks.append(result)
    log(json.dumps(result))
    endpoints = [('netflix', 9080, '/', False),
                 ('cast', 8008, '/setup/eureka_info', False),
                 ('device', 7345, '/state/device/deviceinfo', True),
                 ('power', 7345, '/state/device/power_mode', True),
                 ('airplay', 7000, '/info', False)]
    for name, port, path, tls in endpoints:
        result = {'check': name, 'port': port, 'path': path}
        try:
            status, body = http_get(host, port, path, tls=tls)
            result.update(http_status=status, **summarize(name, body))
        except (OSError, ValueError, KeyError, IndexError, urllib.error.URLError) as exc:
            result['error'] = str(exc)
        checks.append(result)
        log(json.dumps(result))
    return checks


def connect_port(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return port
    except OSError:
        return None


def clean_targets(data):
    """Recognize CDP target discovery without retaining page query tokens."""
    if not isinstance(data, list):
        return []
    targets = []
    for item in data:
        if not isinstance(item, dict):
            continue
        websocket = item.get('webSocketDebuggerUrl')
        if not isinstance(websocket, str) or not websocket.startswith(('ws://', 'wss://')):
            continue
        raw_url = item.get('url', '')
        parsed = urllib.parse.urlsplit(raw_url if isinstance(raw_url, str) else '')
        targets.append({'title': str(item.get('title', ''))[:200],
                        'type': item.get('type'),
                        'url': urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
                                                       parsed.path, '', '')),
                        'debugger_advertised': True})
    return targets


def find_targets(host, port):
    result = {'port': port}
    try:
        status, body = http_get(host, port, '/json/list', timeout=2)
        result['http_status'] = status
        targets = clean_targets(json.loads(body))
        if targets:
            result.update(targets=targets, debugger_page=f'http://{host}:{port}/')
        else:
            result['result'] = 'No advertised CDP targets'
    except (ValueError, OSError, urllib.error.URLError) as exc:
        result['error'] = str(exc)
    return result


def cached_ports(host, path=CACHE):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        ports = data.get(host, [])
        if not isinstance(ports, list):
            return []
        return list(dict.fromkeys(p for p in ports if type(p) is int and 1 <= p <= 65535))
    except (OSError, ValueError, AttributeError):
        return []


def remember_ports(host, ports, path=CACHE):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[host] = list(dict.fromkeys(ports + cached_ports(host, path)))[:20]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temporary.replace(path)


def ordered_ports(first, last, preferred=()):
    """Check recent ports first, then likely ephemeral ports; still cover the range."""
    seen = set()
    for start, end in [(32768, 65535), (1024, 32767), (1, 1023)]:
        if not seen:
            for port in preferred:
                if first <= port <= last and port not in seen:
                    seen.add(port)
                    yield port
        for port in range(max(first, start), min(last, end) + 1):
            if port not in seen:
                seen.add(port)
                yield port


def scan(host, first, last, workers, timeout, full=False, preferred=(), on_found=None):
    opened = []
    probes = []
    checked = 0
    total = last - first + 1
    stop = threading.Event()

    def accept(result):
        probes.append(result)
        if result.get('targets'):
            log('DEVTOOLS FOUND: ' + result['debugger_page'])
            log(json.dumps(result['targets']))
            if on_found:
                on_found(result)

    preferred = list(dict.fromkeys(p for p in preferred if first <= p <= last))
    if preferred:
        log('Checking cached/historical debugger ports: ' + ', '.join(map(str, preferred)))
        # A known working port should not wait for thousands of TCP timeouts.
        with ThreadPoolExecutor(max_workers=min(8, len(preferred))) as quick:
            pending = {quick.submit(find_targets, host, p): p for p in preferred}
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for task in done:
                    port = pending.pop(task)
                    result = task.result()
                    # Only confirmed debugger hits count here. Misses get a TCP
                    # check later, since HTTP silence doesn't mean a port is closed.
                    if result.get('targets'):
                        opened.append(port)
                        checked += 1
                        accept(result)
                        if not full:
                            stop.set()
                if stop.is_set():
                    for task in pending:
                        task.cancel()
                    break

    def probe(port):
        if stop.is_set():
            return None, None
        found = connect_port(host, port, timeout)
        return found, find_targets(host, port) if found is not None else None

    if not stop.is_set():
        log(f'Scanning TCP {first}-{last}, high ports first; {workers} workers; {timeout}s timeout.')
        ports = iter(p for p in ordered_ports(first, last, preferred) if p not in opened)
        pool = ThreadPoolExecutor(max_workers=workers)
        pending = set()
        last_progress = time.monotonic()

        def refill():
            while len(pending) < workers * 2:
                try:
                    pending.add(pool.submit(probe, next(ports)))
                except StopIteration:
                    break

        try:
            refill()
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for task in done:
                    port, result = task.result()
                    checked += 1
                    if port is not None:
                        opened.append(port)
                        log(f'OPEN TCP {port}')
                        accept(result)
                        if result.get('targets') and not full:
                            stop.set()
                if stop.is_set():
                    break
                if time.monotonic() - last_progress >= 10:
                    log(f'Checked {checked}/{total} ports')
                    last_progress = time.monotonic()
                refill()
        finally:
            stop.set()
            for task in pending:
                task.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
    return {'range': [first, last], 'open_tcp_ports': sorted(opened),
            'ports_scanned': checked, 'ports_in_range': total,
            'scan_complete': checked == total, 'stopped_after_debugger': checked < total and bool(
                [p for p in probes if p.get('targets')]),
            'devtools_probes': sorted(probes, key=lambda p: p['port']),
            'debuggers': [p for p in probes if p.get('targets')]}


def port_range(value):
    try:
        parts = value.split('-')
        first, last = (int(parts[0]), int(parts[0])) if len(parts) == 1 else map(int, parts)
        if not 1 <= first <= last <= 65535:
            raise ValueError()
        return first, last
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('Use a port or inclusive range, e.g. 1-65535')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='192.168.50.233', type=ipaddress.IPv4Address)
    parser.add_argument('--bind', type=ipaddress.IPv4Address, help='Optional local interface IPv4')
    parser.add_argument('--ports', type=port_range, default=(1, 65535))
    parser.add_argument('--workers', type=int, choices=range(1, 513), metavar='1-512')
    parser.add_argument('--timeout', type=float, help='TCP timeout (fast: .2s; full: .5s)')
    parser.add_argument('--full', action='store_true', help='Thorough full-range scan; do not stop on discovery')
    parser.add_argument('--replay', action='store_true', help='Repeat original SSDP/status checks before EVERY new search')
    parser.add_argument('--watch', action='store_true', help='Keep checking; rediscover when the debugger disappears')
    parser.add_argument('--interval', type=float, default=2, help='Watch check interval in seconds (default 2)')
    parser.add_argument('--label', default='', help='TV state, e.g. SmartCast home or Paramount')
    parser.add_argument('--output', type=Path, help='JSON report (default: data/rediscover-TIMESTAMP.json)')
    parser.add_argument('--open', action='store_true', help='Open discovered debugger pages in your default browser')
    args = parser.parse_args(argv)
    args.timeout = args.timeout if args.timeout is not None else (.5 if args.full else .2)
    args.workers = args.workers or (128 if args.full else 256)
    if not 0 < args.timeout <= 30:
        parser.error('--timeout must be greater than zero and at most 30')
    if not 0 < args.interval <= 3600:
        parser.error('--interval must be greater than zero and at most 3600')
    host = str(args.host)
    started = time.monotonic()
    stamp = datetime.now(timezone.utc)
    output = args.output or ROOT / 'data' / f'rediscover-{stamp:%Y%m%dT%H%M%S%fZ}.json'
    report = {'host': host, 'label': args.label, 'started_utc': stamp.isoformat(),
              'debug_setting_changed': False, 'status_checks': [], 'completed': False}

    def found(result):
        remember_ports(host, [result['port']])
        # Open immediately, rather than waiting for the scan to finish.
        if args.open:
            webbrowser.open(result['debugger_page'], new=2)

    try:
        while True:
            if args.replay:
                report['status_checks'] = replay(host, str(args.bind) if args.bind else None)
            preferred = cached_ports(host) + HISTORICAL_PORTS
            report['scan'] = scan(host, *args.ports, args.workers, args.timeout,
                                  full=args.full, preferred=preferred, on_found=found)
            if not args.watch:
                break
            report['duration_seconds'] = round(time.monotonic() - started, 1)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            active = report['scan']['debuggers']
            if active:
                log('Watching debugger; Ctrl+C to stop.')
                while active:
                    time.sleep(args.interval)
                    active = [r for d in active if (r := find_targets(host, d['port'])).get('targets')]
                log('Debugger disappeared; searching for its new port...')
            else:
                log('No debugger found; waiting before another search...')
                time.sleep(args.interval)
        report['completed'] = True
    except KeyboardInterrupt:
        report['completed'] = False
        report['error'] = 'Interrupted; scan results may be incomplete.'
    finally:
        report['duration_seconds'] = round(time.monotonic() - started, 1)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        log('Report saved: ' + str(output.resolve()))
    debuggers = report.get('scan', {}).get('debuggers', [])
    if report['completed']:
        log(f'Done: {len(report["scan"]["open_tcp_ports"])} open TCP ports; '
            f'{len(debuggers)} responding debugger listener(s).')
        if not debuggers:
            log('No debugger found. Replaying these requests is not a confirmed way to enable it.')
    return 0 if report['completed'] else 130


if __name__ == '__main__':
    raise SystemExit(main())
