"""Enable native Vizio Talk Back, set its supported speech rate, and speak via CDP.

Uses a running TV app's DevTools connection. Temporary API authorization stays
inside the TV page. Speech text and credentials are not logged or saved.
"""
import argparse
import ipaddress
import json
import math
from pathlib import Path
import socket
import sys

import discover_tvs as tv_discovery
import rediscover_devtools as discovery
from launch_app_devtools import evaluate
from meme_img_plug import choose_target

MAX_TEXT_BYTES = 20 * 1024


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', type=ipaddress.IPv4Address, help='TV LAN address; omit for discovery and TV selection')
    tv_discovery.add_discovery_options(p)
    p.add_argument('--port', type=int, help='Current DevTools port; otherwise discover it')
    p.add_argument('--target', help='Page title/URL substring if multiple pages are exposed')
    state = p.add_mutually_exclusive_group()
    state.add_argument('--enable', action='store_true', help='Turn native Talk Back on')
    state.add_argument('--disable', action='store_true', help='Turn native Talk Back off')
    text = p.add_mutually_exclusive_group()
    text.add_argument('--say', '--text', dest='text', help='Text to speak; enables native Talk Back if needed')
    text.add_argument('--text-file', type=Path, help='Read UTF-8 text, at most 20 KiB')
    text.add_argument('--stop', action='store_true', help='Stop speech without changing the Talk Back setting')
    p.add_argument('--auto-disable', action='store_true', help='Turn Talk Back off after a conservative estimated speech duration (no exact completion signal)')
    p.add_argument('--disable-after', type=float, metavar='SECONDS', help='Turn Talk Back off this many seconds after speech is accepted (1-3600); implies --auto-disable')
    p.add_argument('--rate', '--speed', dest='rate', help='Native speech speed, validated against TV options (Slow/Normal/Fast on V705-H3)')
    p.add_argument('--status', action='store_true', help='Read-only status; cannot be combined with setting changes or speech')
    p.add_argument('--options', action='store_true', help='Read-only status and supported speech-rate choices')
    p.add_argument('--timeout', type=float, default=15, help='TV operation timeout in seconds (1-60)')
    return p


def operation_config(args):
    text = args.text
    if args.text_file:
        try:
            with args.text_file.open('rb') as source:
                data = source.read(MAX_TEXT_BYTES + 1)
            if len(data) > MAX_TEXT_BYTES:
                raise ValueError('Text file exceeds 20 KiB.')
            text = data.decode('utf-8-sig')
        except (OSError, UnicodeError):
            raise ValueError('Cannot read UTF-8 text file.') from None
    if text is not None and (not text.strip() or len(text.encode('utf-8')) > MAX_TEXT_BYTES):
        raise ValueError('Speech text must be nonempty and at most 20 KiB.')
    if args.disable and text is not None:
        raise ValueError('--disable cannot be combined with --say or --text-file.')
    if args.enable and args.stop:
        raise ValueError('--stop does not enable speech; omit --enable.')
    if args.rate is not None and (not args.rate.strip() or len(args.rate) > 100):
        raise ValueError('Supply a speech rate such as Slow, Normal, or Fast.')
    auto_disable = args.auto_disable or args.disable_after is not None
    if auto_disable and text is None:
        raise ValueError('--auto-disable and --disable-after require --say or --text-file.')
    if args.disable_after is not None and (not math.isfinite(args.disable_after) or not 1 <= args.disable_after <= 3600):
        raise ValueError('--disable-after must be between 1 and 3600 seconds.')
    mutating = args.enable or args.disable or text is not None or args.stop or args.rate is not None
    if (args.status or args.options) and mutating:
        raise ValueError('--status and --options are read-only; use them separately from speech/settings changes.')
    return {'enable': bool(args.enable or text is not None), 'disable': args.disable,
            'text': text, 'stop': args.stop, 'rate': args.rate,
            'autoDisable': bool(auto_disable), 'disableAfter': args.disable_after}



def cleanup_wait_budget(config):
    if not config.get('autoDisable'):
        return 0
    if config.get('disableAfter') is not None:
        return config['disableAfter']
    text = config.get('text') or ''
    # Match the JS UTF-16 length and use the slowest estimated rate as an upper bound.
    characters = len(text.encode('utf-16-le')) // 2
    words = max(len(text.split()), characters / 5)
    return max(8, math.ceil(words * 60 / 90 + 3))


def talkback_expression(config, timeout):
    return '''(async()=>{
 const config=OPERATION;
 const base='http://127.0.0.1:7346';
 const root='/menu_native/dynamic/tv_settings/accessibility/';
 const staticRoot='/menu_native/static/tv_settings/accessibility/';
 let controller=new AbortController();
 let timer=setTimeout(()=>controller.abort(),TIME_LIMIT);
 const changes={};let token;let stage='authorization';let writeInFlight=false;
 let result;let autoOffPending=false;let shutoffDelay;
 const fail=(status,extra={})=>{throw {safe:true,status,stage,...extra}};
 const apiCode=d=>typeof d?.STATUS?.RESULT==='string'&&/^[A-Z_]{1,64}$/.test(d.STATUS.RESULT)?d.STATUS.RESULT:'UNKNOWN';
 async function request(path,method='GET',body){
   const writing=method==='PUT';writeInFlight=writing;
   const headers=token?{AUTH:token}:{};
   if(body!==undefined)headers['Content-Type']='application/json';
   const r=await fetch(base+path,{method,headers,credentials:'omit',cache:'no-store',signal:controller.signal,...(body===undefined?{}:{body:JSON.stringify(body)})});
   let d;try{d=await r.json()}catch{fail('invalid_response',{httpStatus:r.status,outcome:writing?'unknown':'not_sent'})}
   const result=apiCode(d);
   if(!r.ok||result!=='SUCCESS')fail('api_error',{httpStatus:r.status,apiResult:result,outcome:writing&&r.status>=500?'unknown':'not_sent'});
   writeInFlight=false;return d;
 }
 const items=d=>d.ITEMS||d.ITEM?.ITEMS||[];
 async function metadata(key){
   stage='read_'+key;const d=await request(root+key);const item=items(d).find(i=>i.CNAME===key);
   if(!item||item.TYPE!=='T_LIST_V1')fail('unsupported_setting');
   return item;
 }
 async function choices(key){
   stage='options_'+key;const d=await request(staticRoot+key);const item=items(d).find(i=>i.CNAME===key);
   if(!item||!Array.isArray(item.ELEMENTS)||!item.ELEMENTS.every(v=>typeof v==='string'))fail('unsupported_options');
   return item.ELEMENTS;
 }
 async function setSetting(key,value){
   const current=await metadata(key);
   if(current.VALUE===value)return;
   if(typeof current.HASHVAL!=='number'||!Number.isFinite(current.HASHVAL))fail('missing_setting_hash');
   stage='set_'+key;
   await request(root+key,'PUT',{REQUEST:'MODIFY',HASHVAL:current.HASHVAL,VALUE:value});
   changes[key]=value;
   const after=await metadata(key);
   if(after.VALUE!==value)fail('setting_not_confirmed');
 }
 try{
   const auth=await request('/app/get_auth_token');token=auth.ITEM?.VALUE;
   if(typeof token!=='string'||!token.length)fail('authorization_unavailable');
   const rates=await choices('speech_rate');
   const talkbackOptions=await choices('talkback');
   let chosenRate;
   if(config.rate!==null){chosenRate=rates.find(r=>r.toLowerCase()===config.rate.trim().toLowerCase());if(!chosenRate)fail('unsupported_rate',{supportedRates:rates});}
   const desired=config.disable?'Off':config.enable?'On':null;
   if(desired!==null&&!talkbackOptions.includes(desired))fail('unsupported_talkback_value');
   if(config.stop){stage='stop_speech';await request('/app/tts/play','PUT',{VALUE:' '});}
   if(chosenRate!==undefined)await setSetting('speech_rate',chosenRate);
   if(config.autoDisable)autoOffPending=true;
   if(desired!==null)await setSetting('talkback',desired);
   const talkback=await metadata('talkback');const rate=await metadata('speech_rate');
   stage='read_native_speech';const native=await request('/app/tts/enabled');
   const enabled=native.ITEM?.VALUE===1||native.ITEM?.VALUE===true;
   let speechRequested=false;
   if(config.text!==null){
     if(talkback.VALUE!=='On'||!enabled)fail('native_speech_disabled');
     stage='speak';await request('/app/tts/play','PUT',{VALUE:config.text});speechRequested=true;
     if(config.autoDisable){
       const effectiveWords=Math.max(config.text.trim().split(/\s+/u).length,config.text.length/5);
       const estimatedWpm={Slow:90,Normal:140,Fast:190}[rate.VALUE]||90;
       shutoffDelay=config.disableAfter!==null?config.disableAfter:Math.max(8,Math.ceil(effectiveWords*60/estimatedWpm+3));
       stage='wait_before_disable';
       clearTimeout(timer); // The intentional playback wait is not a stalled API request.
       await new Promise(resolve=>setTimeout(resolve,shutoffDelay*1000));
     }
   }
   result={ok:true,talkback:talkback.VALUE,nativeSpeechEnabled:enabled,speechRate:rate.VALUE,
     supportedRates:rates,voiceSelection:'not_exposed',pitchControl:'not_exposed',changes,
     speechRequested,speechStopped:!!config.stop,...(speechRequested?{textLength:config.text.length}:{})};
 }catch(e){
   result=e?.safe?{ok:false,...e,changes}:{ok:false,status:'request_interrupted',stage,outcome:writeInFlight?'unknown':'not_sent',changes};
 }finally{
   if(autoOffPending){
     // Attempt cleanup after success or failure, with a fresh timeout/token.
     // A long speech wait can outlive the temporary authorization token.
     clearTimeout(timer);controller=new AbortController();
     timer=setTimeout(()=>controller.abort(),TIME_LIMIT);
     try{
       stage='cleanup_authorization';token=undefined;
       const auth=await request('/app/get_auth_token');token=auth.ITEM?.VALUE;
       if(typeof token!=='string'||!token.length)fail('authorization_unavailable');
       await setSetting('talkback','Off');
       const finalSetting=await metadata('talkback');
       stage='verify_disabled';const finalNative=await request('/app/tts/enabled');
       const nativeEnabled=finalNative.ITEM?.VALUE===1||finalNative.ITEM?.VALUE===true;
       if(finalSetting.VALUE!=='Off'||nativeEnabled)fail('disable_not_confirmed');
       Object.assign(result,{talkback:finalSetting.VALUE,nativeSpeechEnabled:nativeEnabled,autoDisabled:true,
         speechCompletionVerified:false,completionMethod:shutoffDelay===undefined?'cleanup_after_error':config.disableAfter!==null?'fixed_delay':'estimated_delay',
         ...(shutoffDelay===undefined?{}:{disableAfterSeconds:shutoffDelay})});
     }catch(_){Object.assign(result,{ok:false,autoDisabled:false,cleanupFailed:true,cleanupStage:stage});}
   }
   token=undefined;clearTimeout(timer);
 }
 return result;
})()'''.replace('TIME_LIMIT', str(round(timeout * 1000))).replace('OPERATION', json.dumps(config, ensure_ascii=True))


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.host is not None and not tv_discovery.lan_address(args.host):
        p.error('Use the TV private LAN address')
    if args.host is not None and (args.discover or args.list_tvs or args.scan_subnet):
        p.error('Choose --host OR TV discovery options')
    if args.port is not None and not 1 <= args.port <= 65535:
        p.error('--port must be between 1 and 65535')
    if not 1 <= args.discovery_timeout <= 15 or not 1 <= args.timeout <= 60:
        p.error('Discovery timeout must be 1-15 seconds; operation timeout must be 1-60 seconds')
    try:
        if args.scan_subnet:
            tv_discovery.scan_network(args.scan_subnet)
        config = operation_config(args) if not args.list_tvs else None
    except ValueError as exc:
        p.error(str(exc))
    mutation_possible = config and (config['enable'] or config['disable'] or config['text'] is not None or config['stop'] or config['rate'] is not None)
    try:
        if args.host is None:
            print('Discovering Vizio TVs...', flush=True)
            devices, warnings = tv_discovery.discover_tvs(str(args.bind) if args.bind else None,
                                                          args.discovery_timeout, args.scan_subnet)
            for warning in warnings:
                print(warning)
            if args.list_tvs:
                tv_discovery.print_tvs(devices)
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
                print('No debugger found. Open a compatible app such as Hulu and retry.', file=sys.stderr)
                return 1
            port = result['debuggers'][0]['port']
        target = choose_target(host, port, args.target)
        discovery.remember_ports(host, [port])
        execution_timeout = args.timeout + 5 + (cleanup_wait_budget(config) + args.timeout if config.get('autoDisable') else 0)
        with socket.create_connection((host, port), timeout=6) as raw:
            ws = websocket.create_connection(target['webSocketDebuggerUrl'], socket=raw,
                                             timeout=execution_timeout, suppress_origin=True)
            try:
                result = evaluate(ws, talkback_expression(config, args.timeout), 1, execution_timeout)
            finally:
                ws.close()
        # Result excludes credentials, text, native response bodies and page URLs.
        result.pop('safe', None)
        print(json.dumps(result, indent=2))
        if result.get('ok'):
            if result.get('speechRequested'):
                if result.get('autoDisabled'):
                    print('Talk Back disabled after the playback delay. Speech completion was not directly reported by the TV.')
                else:
                    print('TV accepted speech playback. Talk Back remains enabled until you turn it off.')
            return 0
        if result.get('cleanupFailed'):
            print('Automatic shutoff could not be confirmed. Reconnect and run --disable.', file=sys.stderr)
            return 2
        if result.get('outcome') == 'unknown':
            print('Outcome unknown. Check TV/status before retrying; no automatic retry was made.')
            return 2
        return 1
    except tv_discovery.SelectionCancelled as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print('Stopped. In-page cleanup may continue while the app stays open; use --status/--disable to verify.')
        return 130
    except Exception as exc:
        print(f'DevTools connection/evaluation failed ({type(exc).__name__}).', file=sys.stderr)
        if mutation_possible:
            print('Outcome unknown if an operation was sent. Reconnect and read --status before retrying.', file=sys.stderr)
            return 2
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
