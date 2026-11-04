"""Discover Vizio TVs with SSDP, validated saved addresses, and an optional LAN scan."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
from pathlib import Path
import socket
import time

from rediscover_devtools import http_get

ROOT = Path(__file__).resolve().parent
SSDP_TARGETS = ('urn:schemas-kinoma-com:device:shell:1',
                'urn:dial-multiscreen-org:service:dial:1', 'ssdp:all')


class NoTVsFound(ValueError):
    """Discovery found no currently responding TVs; no control command was sent."""


class SelectionCancelled(ValueError):
    pass


def lan_address(value):
    try:
        address = ipaddress.IPv4Address(value)
        if (address.is_private and not address.is_loopback and not address.is_link_local
                and not address.is_multicast and not address.is_unspecified):
            return str(address)
    except (ValueError, TypeError):
        pass
    return None


def scan_network(value):
    network = ipaddress.ip_network(value, strict=False)
    if (network.version != 4 or network.num_addresses > 1024
            or not lan_address(network.network_address) or not lan_address(network.broadcast_address)):
        raise ValueError('Use a private IPv4 subnet of at most 1024 addresses (e.g. 192.168.1.0/24).')
    return network


def saved_hosts():
    hosts = set()
    for name in ('devtools-ports.json', 'tv.json'):
        try:
            data = json.loads((ROOT / '.local' / name).read_text(encoding='utf-8'))
            values = data.keys() if name == 'devtools-ports.json' else [data.get('host')]
            hosts.update(host for value in values if (host := lan_address(value)))
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    return hosts


def ssdp_candidates(bind=None, duration=3):
    """Only use responder IPs; never fetch arbitrary advertised LOCATION URLs."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        if bind is None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
                route.connect(('239.255.255.250', 1900))
                bind = route.getsockname()[0]
        sock.bind((bind, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(.25)
        for target in SSDP_TARGETS:
            message = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
                       'MAN: "ssdp:discover"\r\nMX: 1\r\nST: ' + target + '\r\n\r\n')
            sock.sendto(message.encode('ascii'), ('239.255.255.250', 1900))
        found = set()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            try:
                body, remote = sock.recvfrom(16384)
            except socket.timeout:
                continue
            if body.startswith(b'HTTP/1.1 200') and (host := lan_address(remote[0])):
                found.add(host)
        return found


def clean_label(value):
    return ''.join(c for c in str(value) if c.isprintable())[:100]


def probe_tv(host):
    for port in (7345, 9000):
        try:
            with socket.create_connection((host, port), timeout=.35):
                pass
            _, body = http_get(host, port, '/state/device/deviceinfo', timeout=2, tls=True)
            data = json.loads(body)
            if data.get('STATUS', {}).get('RESULT', '').upper() != 'SUCCESS':
                continue
            for item in data.get('ITEMS', []):
                if 'VIZIO' not in str(item.get('NAME', '')).upper() and not str(item.get('TYPE', '')).startswith('T_VIZIO_DEVICE_INFO'):
                    continue
                value = item.get('VALUE', {})
                if isinstance(value, dict) and value.get('MODEL_NAME'):
                    return {'host': host, 'name': clean_label(value.get('CAST_NAME') or 'Vizio TV'),
                            'model': clean_label(value['MODEL_NAME']), 'api_port': port}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def discover_tvs(bind=None, duration=3, subnet=None):
    sources = {}
    warnings = []
    def add(host, source):
        sources.setdefault(host, set()).add(source)
    # Validate explicit scan limits before any network I/O.
    network = scan_network(subnet) if subnet else None
    for host in saved_hosts():
        add(host, 'saved address')
    try:
        for host in ssdp_candidates(bind, duration):
            add(host, 'SSDP')
    except OSError:
        warnings.append('SSDP discovery failed on this interface; try --bind YOUR_LOCAL_IP.')
    if network:
        for host in network.hosts():
            add(str(host), 'subnet scan')
    devices = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        for device in pool.map(probe_tv, sorted(sources, key=ipaddress.IPv4Address)):
            if device:
                device['discovered_by'] = sorted(sources[device['host']])
                devices.append(device)
    return devices, warnings


def print_tvs(devices):
    for number, device in enumerate(devices, 1):
        print(f"{number}. {device['name']} | {device['model']} | {device['host']} | API {device['api_port']}")


def select_tv(devices, input_fn=None):
    if not devices:
        raise NoTVsFound('No responding Vizio TVs found. Turn the TV on, try --scan-subnet CIDR, or use --host YOUR_TV_IP.')
    print_tvs(devices)
    read = input_fn or input
    while True:
        try:
            answer = read('Select TV number (q to cancel): ').strip()
        except (EOFError, KeyboardInterrupt):
            raise SelectionCancelled('TV selection cancelled. For unattended use, pass --host YOUR_TV_IP.') from None
        if answer.casefold() in ('q', 'quit', 'exit'):
            raise SelectionCancelled('TV selection cancelled; no TV action sent.')
        if answer.isascii() and answer.isdecimal() and 1 <= int(answer) <= len(devices):
            return devices[int(answer) - 1]
        print(f'Enter a number from 1 to {len(devices)}, or q.')


def add_discovery_options(p):
    p.add_argument('--discover', action='store_true', help='Discover TVs and show a selection menu (also used when --host is omitted)')
    p.add_argument('--list-tvs', action='store_true', help='List responding Vizio TVs and exit without launching')
    p.add_argument('--bind', type=ipaddress.IPv4Address, help='Local interface IPv4 for SSDP')
    p.add_argument('--scan-subnet', help='Also check a private subnet for non-advertising TVs, e.g. 192.168.1.0/24 (max 1024 addresses)')
    p.add_argument('--discovery-timeout', type=float, default=3, help='SSDP response window in seconds (1-15)')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    add_discovery_options(p)
    p.add_argument('--select', action='store_true', help='Show a numbered TV selection menu')
    p.add_argument('--json', action='store_true', help='Output device metadata as JSON')
    args = p.parse_args(argv)
    if not 1 <= args.discovery_timeout <= 15:
        p.error('--discovery-timeout must be between 1 and 15')
    if args.json and args.select:
        p.error('Choose --json or --select')
    try:
        devices, warnings = discover_tvs(str(args.bind) if args.bind else None,
                                         args.discovery_timeout, args.scan_subnet)
        if args.json:
            print(json.dumps({'tvs': devices, 'warnings': warnings}, indent=2))
        else:
            for warning in warnings:
                print(warning)
            if args.select:
                selected = select_tv(devices)
                print(f"Selected {selected['name']} at {selected['host']}")
            else:
                print_tvs(devices)
                if not devices:
                    print('No responding Vizio TVs found. Try --scan-subnet CIDR or turn the TV on.')
        return 0 if devices else 1
    except (ValueError, SelectionCancelled) as exc:
        print(str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
