"""Cast a video URL or local file to a Vizio TV over Google Cast, without SmartCast PINs.

No DevTools connection, pairing credential, or browser window is used. The TV
fetches the media URL. Signed URLs are passed to the TV but never printed.
"""
import argparse
import ipaddress
import json
import logging
import math
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
import urllib.request

import discover_tvs as tv_discovery
from cast_media_server import MediaFileServer

ROOT = Path(__file__).resolve().parent
DEMO_URL = 'https://media.w3.org/2010/05/sintel/trailer.mp4'
MEDIA_NAMESPACE = 'urn:x-cast:com.google.cast.media'
MIME_TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.webm': 'video/webm',
              '.m3u8': 'application/x-mpegURL', '.mpd': 'application/dash+xml'}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', type=ipaddress.IPv4Address, help='TV LAN address; omit for TV discovery/selection')
    tv_discovery.add_discovery_options(p)
    action = p.add_mutually_exclusive_group()
    action.add_argument('--url', help='Direct HTTP(S) video/stream URL reachable by the TV')
    action.add_argument('--file', type=Path, help='Serve one local video file to the TV; keep this script running')
    action.add_argument('--demo', action='store_true', help='Download/cache the public Sintel trailer and serve it locally to the TV')
    action.add_argument('--status', action='store_true', help='Read Cast status without launching an app')
    action.add_argument('--stop', action='store_true', help='Stop the current Cast media session')
    p.add_argument('--serve-seconds', type=float, help='Optional --file/--demo serving duration after playback starts; otherwise serve until finished or Ctrl+C')
    p.add_argument('--content-type', '--mime', help='Override inferred MIME type, e.g. video/mp4')
    p.add_argument('--live', action='store_true', help='Use LIVE stream type instead of BUFFERED')
    p.add_argument('--title', default='VizioControl video', help='Title shown in the receiver UI')
    p.add_argument('--cast-port', '--port', dest='cast_port', type=int, default=8009, help='Cast TCP port (default 8009; not a DevTools port)')
    p.add_argument('--timeout', type=float, default=30, help='Connection/playback confirmation timeout, seconds (1-120)')
    return p


def media_config(args):
    if args.list_tvs:
        return None
    if not (args.url or args.file or args.demo or args.status or args.stop):
        raise ValueError('Choose --url URL, --file FILE, --demo, --status, --stop, or --list-tvs.')
    if args.serve_seconds is not None and not (args.file or args.demo):
        raise ValueError('--serve-seconds requires --file or --demo.')
    if args.status or args.stop:
        if args.content_type or args.live:
            raise ValueError('--content-type and --live apply only to video playback.')
        return None
    local_file = None
    if args.file:
        try:
            local_file = args.file.resolve(strict=True)
            if not local_file.is_file() or local_file.stat().st_size == 0:
                raise OSError
        except OSError:
            raise ValueError('Choose an existing nonempty media file.') from None
    url = DEMO_URL if args.demo else args.url
    if local_file:
        url = 'http://local-file.invalid/' + local_file.name
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
            raise ValueError
        _ = parsed.port
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
    except ValueError:
        raise ValueError('Use a valid direct HTTP(S) media URL without embedded username/password. Signed query URLs are supported.') from None
    content_type = args.content_type
    if content_type is None:
        path = local_file.name.lower() if local_file else parsed.path.lower()
        content_type = next((mime for extension, mime in MIME_TYPES.items() if path.endswith(extension)), None)
        if content_type is None:
            raise ValueError('Cannot infer video MIME type. Add --content-type, e.g. video/mp4 or application/x-mpegURL.')
    if not re.fullmatch(r'[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+', content_type):
        raise ValueError('Use a MIME type such as video/mp4, without extra parameters.')
    return {'url': None if local_file else url, 'file': local_file, 'demo': args.demo, 'content_type': content_type, 'title': args.title,
            'stream_type': 'LIVE' if args.live else 'BUFFERED'}


def download_demo(cache_path=None):
    path = cache_path or ROOT / '.local' / 'cast-demo-sintel.mp4'
    if path.is_file() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    try:
        with urllib.request.urlopen(DEMO_URL, timeout=20) as response, temporary.open('wb') as output:
            size = 0
            while block := response.read(65536):
                size += len(block)
                if size > 32 * 1024 * 1024:
                    raise ValueError('Demo exceeds download limit')
                output.write(block)
        if temporary.stat().st_size == 0:
            raise ValueError('Demo download was empty')
        temporary.replace(path)
        return path
    finally:
        if temporary.exists():
            temporary.unlink()


class PlaybackEvents:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.status = None
        self.error = None

    def new_media_status(self, status):
        with self.lock:
            self.status = status
            self.event.set()

    def load_media_failed(self, _item, error_code):
        with self.lock:
            self.error = {'status': 'media_load_failed'}
            if isinstance(error_code, int):
                self.error['errorCode'] = error_code
            self.event.set()

    def new_launch_error(self, _failure):
        with self.lock:
            self.error = {'status': 'receiver_launch_failed'}
            self.event.set()

    def wait_for(self, url, timeout, stopping=False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                status, error = self.status, self.error
                self.event.clear()
            if error:
                return {'ok': False, **error}
            if status is not None:
                state = getattr(status, 'player_state', None)
                if stopping and state == 'IDLE':
                    return {'ok': True, 'status': 'stopped', 'playerState': state}
                # An old/current app reporting PLAYING is not proof our URL loaded.
                if not stopping and getattr(status, 'content_id', None) == url:
                    if state == 'PLAYING':
                        return {'ok': True, 'status': 'playing', 'playerState': state,
                                'contentType': getattr(status, 'content_type', None)}
                    if state == 'IDLE' and getattr(status, 'idle_reason', None) == 'ERROR':
                        return {'ok': False, 'status': 'media_playback_error'}
            self.event.wait(min(.5, max(0, deadline - time.monotonic())))
        return {'ok': False, 'status': 'confirmation_timeout', 'outcome': 'unknown'}


def create_cast(host, port, timeout):
    import pychromecast
    from pychromecast.models import CastInfo, ServiceInfo
    # Prevent library diagnostics from printing media URLs or device identifiers.
    for name in ('pychromecast', 'zeroconf'):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    # We already know this is a display receiver. Avoid an extra identity request
    # and mDNS dependency when the user supplied a specific TV IP.
    info = CastInfo({ServiceInfo('host', (host, port))}, None, 'Vizio TV', 'Vizio TV',
                    host, port, 'cast', 'Vizio')
    return pychromecast.Chromecast(info, tries=1, timeout=min(timeout, 10), retry_wait=1)


def cast_status(cast, listener, timeout):
    receiver = cast.status
    namespaces = getattr(receiver, 'namespaces', []) or []
    active = MEDIA_NAMESPACE in namespaces
    if active:
        cast.media_controller.update_status()
        listener.event.wait(min(timeout, 3))
    status = listener.status
    return {'ok': True, 'status': 'connected', 'castPort': cast.cast_info.port,
            'receiverApp': getattr(receiver, 'display_name', None),
            'receiverAppId': getattr(receiver, 'app_id', None), 'mediaActive': active,
            'playerState': getattr(status, 'player_state', None) if active else None}


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.host is not None and not tv_discovery.lan_address(args.host):
        p.error('Use the TV private LAN address')
    if args.host is not None and (args.discover or args.list_tvs or args.scan_subnet):
        p.error('Choose --host OR TV discovery options')
    if not 1 <= args.cast_port <= 65535 or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 120:
        p.error('Use a valid Cast port and a timeout between 1 and 120 seconds')
    if not 1 <= args.discovery_timeout <= 15:
        p.error('--discovery-timeout must be between 1 and 15')
    try:
        if args.scan_subnet:
            tv_discovery.scan_network(args.scan_subnet)
        if args.serve_seconds is not None and (not math.isfinite(args.serve_seconds) or not 1 <= args.serve_seconds <= 86400):
            raise ValueError('--serve-seconds must be between 1 and 86400.')
        media = media_config(args)
    except ValueError as exc:
        p.error(str(exc))
    cast = None
    media_server = None
    sent = False
    phase = 'TV discovery'
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
        if media and media.get('demo'):
            phase = 'demo download'
            print('Preparing the public sample video in the ignored local cache.', flush=True)
            media['file'] = download_demo()
        if media and media.get('file'):
            phase = 'local media server'
            media_server = MediaFileServer(media['file'], host, media['content_type'],
                                          str(args.bind) if args.bind else None).start()
            media['url'] = media_server.url
            print('Serving only the selected video file over LAN HTTP.', flush=True)
        phase = 'Cast connection'
        try:
            cast = create_cast(host, args.cast_port, args.timeout)
        except ImportError:
            print('Install dependency: python -m pip install "PyChromecast>=13.1,<14"', file=sys.stderr)
            return 1
        listener = PlaybackEvents()
        cast.media_controller.register_status_listener(listener)
        cast.register_launch_error_listener(listener)
        cast.wait(timeout=args.timeout)
        if cast.status is None:
            print('Cast receiver did not respond. Check the TV address, Cast port, and network.', file=sys.stderr)
            return 1
        if args.status:
            result = cast_status(cast, listener, args.timeout)
        elif args.stop:
            if MEDIA_NAMESPACE not in (getattr(cast.status, 'namespaces', []) or []):
                print(json.dumps({'ok': False, 'status': 'no_active_cast_media'}))
                return 1
            phase = 'Cast stop'
            cast.media_controller.update_status()
            listener.event.wait(min(args.timeout, 3))
            state = getattr(listener.status, 'player_state', None)
            if state == 'IDLE':
                result = {'ok': True, 'status': 'already_stopped', 'playerState': state}
            elif getattr(listener.status, 'media_session_id', None) is None:
                print(json.dumps({'ok': False, 'status': 'no_active_media_session'}))
                return 1
            else:
                sent = True
                cast.media_controller.stop()
                result = listener.wait_for(None, args.timeout, stopping=True)
        else:
            phase = 'Cast video load'
            print('Requesting video playback through Google Cast (no SmartCast PIN or DevTools).', flush=True)
            sent = True
            cast.media_controller.play_media(media['url'], media['content_type'],
                title=media['title'], autoplay=True, stream_type=media['stream_type'])
            result = listener.wait_for(media['url'], args.timeout)
        print(json.dumps(result, indent=2))
        if result.get('ok'):
            if media:
                print('TV reports the requested video is PLAYING.', flush=True)
                if media_server:
                    print('Keeping the local video server running; Ctrl+C stops serving.', flush=True)
                    until = time.monotonic() + args.serve_seconds if args.serve_seconds else None
                    while until is None or time.monotonic() < until:
                        state = getattr(listener.status, 'player_state', None)
                        active_url = getattr(listener.status, 'content_id', None)
                        if state == 'IDLE' or (active_url and active_url != media['url']):
                            break
                        if MEDIA_NAMESPACE not in (getattr(cast.status, 'namespaces', []) or []):
                            break
                        listener.event.wait(.5)
                        listener.event.clear()
                else:
                    print('Playback continues after this script exits.')
            return 0
        if media_server and not result.get('ok'):
            print(json.dumps({'localFileRequests': media_server.request_count, 'localBytesSent': media_server.bytes_sent}))
        if result.get('outcome') == 'unknown':
            print('No matching playback confirmation received. Check the TV before retrying; no automatic reload was made.')
            return 2
        return 1
    except (tv_discovery.NoTVsFound, tv_discovery.SelectionCancelled) as exc:
        print(str(exc), file=sys.stderr)
        print('No Cast playback request sent.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Stopped. Cast playback may continue; use --stop to end it.' if sent else 'Stopped. No playback request sent.')
        return 130
    except Exception as exc:
        # No raw exception message: it can contain a signed URL or receiver data.
        print(f'{phase} failed ({type(exc).__name__}).', file=sys.stderr)
        if sent:
            print('Outcome unknown. Check the TV before retrying; no automatic reload was made.', file=sys.stderr)
            return 2
        print('No playback request sent.', file=sys.stderr)
        return 1
    finally:
        if media_server is not None:
            media_server.close()
        if cast is not None:
            try:
                cast.disconnect(timeout=3)
            except Exception:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
