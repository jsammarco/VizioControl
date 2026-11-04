"""Launch a Vizio app through the current page's VIZIO.launchApp native bridge.

Namespace AND app ID are required unless a named catalog preset supplies both.
No pairing tokens are extracted. No HTTP control API fallback is used.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import socket
import sys
import time

import rediscover_devtools as discovery
from meme_img_plug import choose_target
import discover_tvs as tv_discovery

CATALOG = Path(__file__).with_name('app_launch_catalog.json')
CHECK_EXPRESSION = "({ok:true,bridgeAvailable:typeof globalThis.VIZIO?.launchApp==='function'})"


def load_catalog():
    return json.loads(CATALOG.read_text(encoding='utf-8'))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', type=ipaddress.IPv4Address, help='TV LAN address; omit for discovery/selection')
    tv_discovery.add_discovery_options(p)
    p.add_argument('--port', type=int, help='Current DevTools port; otherwise discover it')
    p.add_argument('--target', help='Page title/URL substring if several pages are exposed')
    p.add_argument('--namespace', '--name-space', dest='namespace', type=int,
                   help='NAME_SPACE; required with --app-id')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--app-id', help='APP_ID as text, including hexadecimal Cast IDs')
    group.add_argument('--app', help='Case-insensitive preset name, e.g. "Prime Video"')
    p.add_argument('--message', help='Optional launch MESSAGE, including a URL or JSON string')
    p.add_argument('--list-apps', action='store_true', help='Print supplied presets without connecting')
    p.add_argument('--check', action='store_true', help='Check bridge support without launching anything')
    p.add_argument('--timeout', type=float, default=10, help='Callback timeout in seconds (1-60)')
    return p


def launch_config(args):
    if args.app:
        if args.namespace is not None:
            raise ValueError('Use --app preset OR --namespace with --app-id, not both.')
        found = [a for a in load_catalog() if a['name'].casefold() == args.app.casefold()]
        if not found:
            raise ValueError('Unknown preset. See --list-apps, or supply --namespace and --app-id.')
        config = {k: found[0][k] for k in ('NAME_SPACE', 'APP_ID', 'MESSAGE')}
        if args.message is not None:
            config['MESSAGE'] = args.message
    elif args.app_id is not None and args.namespace is not None:
        config = {'NAME_SPACE': args.namespace, 'APP_ID': args.app_id, 'MESSAGE': args.message}
    elif args.check and args.namespace is None and args.app_id is None and args.message is None:
        return None
    else:
        raise ValueError('Supply BOTH --namespace and --app-id, or select --app NAME.')
    if not 0 <= config['NAME_SPACE'] <= 65535:
        raise ValueError('NAME_SPACE must be between 0 and 65535.')
    if not isinstance(config['APP_ID'], str) or not config['APP_ID'].strip() or len(config['APP_ID']) > 256:
        raise ValueError('APP_ID must be nonempty text, at most 256 characters.')
    message = config['MESSAGE']
    if message is not None and (not isinstance(message, str) or len(message.encode('utf-8')) > 20480):
        raise ValueError('MESSAGE must be text, at most 20 KiB.')
    return config


def launch_expression(config, timeout):
    # Encode all user input as JSON data, never as executable JavaScript.
    data = json.dumps(config, ensure_ascii=True)
    milliseconds = round(timeout * 1000)
    return '''(() => new Promise(resolve => {
 const config = CONFIG;
 if (typeof globalThis.VIZIO?.launchApp !== 'function') {
   resolve({ok:false,status:'unsupported'}); return;
 }
 let done=false;
 const finish=result=>{if(!done){done=true;clearTimeout(timer);resolve(result);}};
 const timer=setTimeout(()=>finish({ok:false,status:'timeout',outcome:'unknown'}), TIMEOUT);
 try {
   const pending=VIZIO.launchApp(config.NAME_SPACE,config.APP_ID,config.MESSAGE,status=>{
     if(status==='SUCCESS') finish({ok:true,status:'accepted'});
     else if(status==='FAILURE') finish({ok:false,status:'rejected'});
     else finish({ok:false,status:'unexpected_callback',outcome:'unknown'});
   });
   if(pending && typeof pending.catch==='function')
     pending.catch(()=>finish({ok:false,status:'bridge_error',outcome:'unknown'}));
 } catch (_) {finish({ok:false,status:'bridge_error',outcome:'unknown'});}
}))()'''.replace('TIMEOUT', str(milliseconds)).replace('CONFIG', data)


def evaluate(ws, expression, command_id, timeout):
    ws.send(json.dumps({'id': command_id, 'method': 'Runtime.evaluate', 'params': {
        'expression': expression, 'awaitPromise': True, 'returnByValue': True}}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ws.settimeout(max(.1, deadline - time.monotonic()))
        raw = ws.recv()
        if not raw:
            raise ConnectionError('Debugger closed')
        message = json.loads(raw)
        if message.get('id') != command_id:
            continue
        if message.get('error') or message.get('result', {}).get('exceptionDetails'):
            raise ValueError('Debugger evaluation failed')
        value = message.get('result', {}).get('result', {}).get('value')
        if not isinstance(value, dict):
            raise ValueError('Unexpected debugger reply')
        return value
    raise TimeoutError('Debugger response timeout')


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.list_apps:
        print(json.dumps(load_catalog(), indent=2))
        return 0
    if args.host is not None and not tv_discovery.lan_address(args.host):
        p.error('Use the TV private LAN address')
    if args.host is not None and (args.discover or args.list_tvs or args.scan_subnet):
        p.error('Choose --host OR TV discovery options')
    if not 1 <= args.discovery_timeout <= 15:
        p.error('--discovery-timeout must be between 1 and 15')
    if args.port is not None and not 1 <= args.port <= 65535:
        p.error('--port must be between 1 and 65535')
    if not 1 <= args.timeout <= 60:
        p.error('--timeout must be between 1 and 60 seconds')
    try:
        if args.scan_subnet:
            tv_discovery.scan_network(args.scan_subnet)
        config = None if args.list_tvs else launch_config(args)
    except ValueError as exc:
        p.error(str(exc))
    sent = False
    try:
        if args.host is None:
            print('Discovering Vizio TVs (SSDP and validated saved addresses)...', flush=True)
            devices, warnings = tv_discovery.discover_tvs(
                str(args.bind) if args.bind else None, args.discovery_timeout, args.scan_subnet)
            for warning in warnings:
                print(warning)
            if args.list_tvs:
                tv_discovery.print_tvs(devices)
                if not devices:
                    print('No responding TVs found. Try --scan-subnet CIDR.')
                return 0 if devices else 1
            host = tv_discovery.select_tv(devices)['host']
        else:
            host = str(args.host)
        try:
            import websocket
        except ImportError:
            print('Install dependency: python -m pip install websocket-client', file=sys.stderr)
            return 1
        port = args.port
        if port is None:
            result = discovery.scan(host, 1, 65535, 256, .2,
                preferred=discovery.cached_ports(host) + discovery.HISTORICAL_PORTS)
            if not result['debuggers']:
                print('No debugger found. Open an app on the TV and retry.', file=sys.stderr)
                return 1
            port = result['debuggers'][0]['port']
        target = choose_target(host, port, args.target)
        discovery.remember_ports(host, [port])
        with socket.create_connection((host, port), timeout=6) as raw:
            ws = websocket.create_connection(target['webSocketDebuggerUrl'], socket=raw,
                                             timeout=args.timeout + 5, suppress_origin=True)
            try:
                check = evaluate(ws, CHECK_EXPRESSION, 1, 6)
                if not check.get('bridgeAvailable'):
                    print('This page does not expose VIZIO.launchApp; nothing launched.', file=sys.stderr)
                    return 1
                if args.check:
                    print('VIZIO.launchApp is available. Check only; no launch sent.')
                    if config:
                        print(f"Selected NAME_SPACE={config['NAME_SPACE']}, APP_ID={config['APP_ID']}; MESSAGE omitted from output.")
                    return 0
                print(f"Launching NAME_SPACE={config['NAME_SPACE']}, APP_ID={config['APP_ID']} through DevTools.", flush=True)
                expression = launch_expression(config, args.timeout)
                sent = True
                result = evaluate(ws, expression, 2, args.timeout + 5)
            finally:
                ws.close()
        print(json.dumps(result, indent=2))
        if result.get('ok'):
            print('TV bridge accepted the launch. Availability/playback was not independently verified.')
            return 0
        if result.get('outcome') == 'unknown':
            print('Launch outcome unknown. Check the TV before retrying; no automatic retry was made.')
            return 2
        return 1
    except tv_discovery.SelectionCancelled as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print('Stopped. Check the TV if a launch was already sent.')
        return 130
    except Exception as exc:
        # Omit page URLs, session state, launch messages, and vendor stack traces.
        print(f'Connection/evaluation failed ({type(exc).__name__}).', file=sys.stderr)
        if sent:
            print('Launch outcome unknown: switching apps can close DevTools before its reply. Check the TV before retrying.', file=sys.stderr)
            return 2
        print('No launch sent. Rediscover the debugger or check the selected page.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
