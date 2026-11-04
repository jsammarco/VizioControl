"""Send TV remote buttons with DevTools or an explicit PIN-paired SmartCast mode.

No power-state queries or Wake-on-LAN. Pairing always requires confirmation.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import socket
import sys

import discover_tvs as tv_discovery
import paired_remote
import rediscover_devtools as discovery
from cdp_key_session import KeySession
from meme_img_plug import choose_target
from vizio import KEYS

BUTTONS = {**KEYS, 'cycle-input': (7, 1), 'channel-down': (8, 0),
           'channel-up': (8, 1), 'previous-channel': (8, 2)}
ACTIONS = ('KEYPRESS', 'KEYDOWN', 'KEYUP')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', type=ipaddress.IPv4Address, help='TV LAN address; omit for TV selection')
    tv_discovery.add_discovery_options(p)
    p.add_argument('--transport', choices=('devtools', 'paired'), default='devtools', help='DevTools key endpoint or explicitly selected PIN-paired HTTPS control')
    p.add_argument('--pair', action='store_true', help='Request/restart pairing only after a y/N confirmation; paired transport only')
    p.add_argument('--config', type=Path, help='Paired credential file (default: ignored per-TV .local file)')
    p.add_argument('--api-port', type=int, help='Paired HTTPS API port; normally 7345')
    p.add_argument('--port', type=int, help='Current DevTools port; otherwise discover it')
    p.add_argument('--target', help='Page title/URL substring if multiple pages are exposed')
    source = p.add_mutually_exclusive_group()
    source.add_argument('--button', '--buttons', nargs='+', choices=sorted(BUTTONS), help='One or more named remote buttons')
    source.add_argument('--codeset', type=int, help='Raw CODESET; requires --code')
    source.add_argument('--sequence-file', type=Path, help='JSON KEYLIST array or object containing KEYLIST')
    p.add_argument('--code', type=int, help='Raw CODE, used with --codeset')
    p.add_argument('--action', choices=ACTIONS, default='KEYPRESS', help='Action for named/raw buttons (default KEYPRESS)')
    p.add_argument('--list-buttons', action='store_true', help='List supported named buttons without connecting')
    p.add_argument('--check', action='store_true', help='Verify the key endpoint using an empty KEYLIST; no buttons sent')
    p.add_argument('--timeout', type=float, default=10, help='TV request timeout in seconds (1-60)')
    return p


def validate_keys(keys):
    if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
        raise ValueError('KEYLIST must contain 1 to 100 key objects.')
    normalized = []
    for item in keys:
        if not isinstance(item, dict) or set(item) != {'CODESET', 'CODE', 'ACTION'}:
            raise ValueError('Each key requires exactly CODESET, CODE, and ACTION.')
        if type(item['CODESET']) is not int or not 0 <= item['CODESET'] <= 255:
            raise ValueError('CODESET must be an integer from 0 to 255.')
        if type(item['CODE']) is not int or not 0 <= item['CODE'] <= 65535:
            raise ValueError('CODE must be an integer from 0 to 65535.')
        if item['ACTION'] not in ACTIONS:
            raise ValueError('ACTION must be KEYPRESS, KEYDOWN, or KEYUP.')
        normalized.append(dict(item))
    return normalized


def key_config(args):
    if args.code is not None and args.codeset is None:
        raise ValueError('--code requires --codeset.')
    if args.sequence_file:
        if args.action != 'KEYPRESS':
            raise ValueError('Set each ACTION in the sequence file instead of using --action.')
        try:
            with args.sequence_file.open('rb') as source:
                data = source.read(65537)
            if len(data) > 65536:
                raise ValueError('Sequence file exceeds 64 KiB.')
            keys = json.loads(data)
        except OSError:
            raise ValueError('Cannot read sequence file.') from None
        except (ValueError, UnicodeError):
            raise ValueError('Sequence file must be valid JSON, at most 64 KiB.') from None
        if isinstance(keys, dict):
            if set(keys) != {'KEYLIST'}:
                raise ValueError('Sequence object must contain only KEYLIST.')
            keys = keys['KEYLIST']
    elif args.button:
        keys = [{'CODESET': BUTTONS[name][0], 'CODE': BUTTONS[name][1], 'ACTION': args.action}
                for name in args.button]
    elif args.codeset is not None:
        if args.code is None:
            raise ValueError('--codeset requires --code.')
        keys = [{'CODESET': args.codeset, 'CODE': args.code, 'ACTION': args.action}]
    elif args.check or args.pair:
        return []
    else:
        raise ValueError('Choose --button NAME, --codeset N --code N, or --sequence-file FILE.')
    return validate_keys(keys)


def key_expression(keys, timeout, check=False):
    # Requests execute inside the TV app. Only sanitized status is printed.
    # Never log CDP interception events or expose authorization in evaluation results.
    return '''(async()=>{
 const keys=KEY_DATA;
 const checkOnly=CHECK_ONLY;
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),TIME_LIMIT);
 let sent=false;
 let token;
 try {
   const auth=await fetch('http://127.0.0.1:7346/app/get_auth_token',{
     cache:'no-store',credentials:'omit',signal:controller.signal});
   if(!auth.ok) return {ok:false,status:'authorization_unavailable',httpStatus:auth.status};
   const data=await auth.json();
   token=data?.ITEM?.VALUE;
   if(data?.STATUS?.RESULT!=='SUCCESS'||typeof token!=='string'||!token.length)
     return {ok:false,status:'authorization_unavailable'};
   sent=!checkOnly;
   const response=await fetch('http://127.0.0.1:7346/key_command/',{
     method:'PUT',credentials:'omit',cache:'no-store',signal:controller.signal,
     headers:{'Content-Type':'application/json','AUTH':token},
     body:JSON.stringify({KEYLIST:checkOnly?[]:keys})});
   if(response.status>=500) return {ok:false,status:'key_endpoint_error',httpStatus:response.status,outcome:sent?'unknown':'not_sent',keysSent:checkOnly?0:undefined};
   let reply;
   try{reply=await response.json();}catch{
     return {ok:false,status:'invalid_response',outcome:sent?'unknown':'not_sent',httpStatus:response.status};
   }
   if(response.ok&&reply?.STATUS?.RESULT==='SUCCESS')
     return checkOnly?{ok:true,status:'available',keysSent:0,httpStatus:response.status}:{ok:true,status:'accepted',count:keys.length,httpStatus:response.status};
   return {ok:false,status:'rejected',httpStatus:response.status};
 }catch(_){return {ok:false,status:sent?'request_interrupted':'authorization_unavailable',outcome:sent?'unknown':'not_sent'};}
 finally{token=undefined;clearTimeout(timer);}
})()'''.replace('TIME_LIMIT', str(round(timeout * 1000))).replace('CHECK_ONLY', 'true' if check else 'false').replace('KEY_DATA', json.dumps(keys))


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.list_buttons:
        for name, (codeset, code) in sorted(BUTTONS.items()):
            print(f'{name:20} CODESET={codeset:2} CODE={code}')
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
        keys = [] if args.list_tvs else key_config(args)
    except ValueError as exc:
        p.error(str(exc))
    if args.transport == 'devtools' and (args.pair or args.config is not None or args.api_port is not None):
        p.error('--pair, --config, and --api-port require --transport paired')
    if args.transport == 'paired' and (args.port is not None or args.target):
        p.error('--port and --target are DevTools options; use --api-port for paired HTTPS')
    if args.api_port is not None and not 1 <= args.api_port <= 65535:
        p.error('--api-port must be between 1 and 65535')
    command_sent = False
    try:
        if args.host is None:
            print('Discovering Vizio TVs...', flush=True)
            devices, warnings = tv_discovery.discover_tvs(
                str(args.bind) if args.bind else None, args.discovery_timeout, args.scan_subnet)
            for warning in warnings:
                print(warning)
            if args.list_tvs:
                tv_discovery.print_tvs(devices)
                return 0 if devices else 1
            host = tv_discovery.select_tv(devices)['host']
        else:
            host = str(args.host)
        if args.transport == 'paired':
            path, config = paired_remote.load_config(host, args.config, args.api_port)
            if args.check and not config.get('auth_token') and not args.pair:
                print('Not paired. To request pairing, run with --transport paired --pair; confirmation is required.')
                return 1
            tv = paired_remote.pair_if_needed(path, config, force=args.pair)
            if args.check:
                tv.request('/app/current', paired=True)
                print('Paired API check succeeded. No buttons sent; no power-state query made.')
                return 0
            if not keys:
                print('Pairing complete; no buttons requested.')
                return 0
            result = paired_remote.send_keys(tv, keys)
            print(json.dumps(result, indent=2))
            if result.get('ok'):
                return 0
            if result.get('outcome') == 'unknown':
                print('Button outcome unknown. Check the TV before retrying; no automatic retry was made.')
                return 2
            print('TV rejected the paired request. Check the credential; use --pair to explicitly request a new pairing.')
            return 1
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
            session = KeySession(ws)
            try:
                session.enable()
                readiness = session.evaluate(key_expression([], args.timeout, True), args.timeout + 5)
                if not readiness.get('ok'):
                    print(json.dumps(readiness, indent=2))
                    print('TV-local key endpoint unavailable; no buttons sent. Use --transport paired for PIN-authorized global buttons; pairing requires confirmation.', file=sys.stderr)
                    return 1
                if args.check:
                    result = readiness
                else:
                    command_sent = True
                    result = session.evaluate(key_expression(keys, args.timeout), args.timeout + 5)
            finally:
                session.close()
                ws.close()
        print(json.dumps(result, indent=2))
        if result.get('ok'):
            print('Check only; no buttons sent.' if args.check else 'TV key-command API accepted the button request.')
            return 0
        if result.get('outcome') == 'unknown':
            print('Button outcome unknown. Check the TV before retrying; no automatic retry was made.')
            return 2
        print('The page could not authorize the request or the TV rejected it.', file=sys.stderr)
        return 1
    except (tv_discovery.SelectionCancelled, paired_remote.PairingDeclined) as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print('Stopped. A command already sent may still take effect.')
        return 130
    except Exception as exc:
        print(f'Connection/evaluation failed ({type(exc).__name__}).', file=sys.stderr)
        if command_sent:
            print('Button outcome unknown. Home, input, or power commands can close DevTools; no automatic retry was made.', file=sys.stderr)
            return 2
        print('No buttons sent. Rediscover the debugger or check the selected page.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
