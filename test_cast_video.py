import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit

import cast_video as video
from cast_media_server import MediaFileServer,byte_range


class CastVideoTests(unittest.TestCase):
    def media(self,*args):
        return video.media_config(video.parser().parse_args(args))

    def test_mime_inference_preserves_signed_url_without_printing_it(self):
        url='https://example.com/clip.mp4?signature=FAKE_SIGNED_VALUE'
        config=self.media('--url',url)
        self.assertEqual(config['url'],url)
        self.assertEqual(config['content_type'],'video/mp4')
        self.assertEqual(self.media('--url','https://example.com/live.m3u8','--live')['stream_type'],'LIVE')

    def test_unknown_type_needs_explicit_override(self):
        with self.assertRaises(ValueError):self.media('--url','https://example.com/watch')
        self.assertEqual(self.media('--url','https://example.com/watch','--mime','video/mp4')['content_type'],'video/mp4')
        for url in ['file:///private/movie.mp4','ftp://example.com/movie.mp4','https://user:password@example.com/movie.mp4']:
            with self.assertRaises(ValueError):self.media('--url',url)

    def test_local_file_validation_and_hash_in_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'clip#one.mp4';p.write_bytes(b'video bytes')
            config=self.media('--file',str(p))
            self.assertEqual(config['content_type'],'video/mp4')
            self.assertEqual(config['file'],p.resolve())
            self.assertIsNone(config['url'])
        with self.assertRaises(ValueError):self.media('--file','does-not-exist.mp4')

    def test_options_cannot_silently_apply_to_status(self):
        for args in [('--status','--live'),('--status','--serve-seconds','5'),()]:
            with self.assertRaises(ValueError):self.media(*args)

    def test_playing_must_match_requested_media(self):
        listener=video.PlaybackEvents()
        listener.new_media_status(SimpleNamespace(player_state='PLAYING',content_id='https://example.com/old.mp4'))
        result=listener.wait_for('https://example.com/new.mp4',.01)
        self.assertEqual(result['outcome'],'unknown')
        listener.new_media_status(SimpleNamespace(player_state='PLAYING',content_id='https://example.com/new.mp4',content_type='video/mp4'))
        self.assertTrue(listener.wait_for('https://example.com/new.mp4',.01)['ok'])

    def test_receiver_load_and_launch_failures_are_not_success(self):
        listener=video.PlaybackEvents();listener.load_media_failed(None,104)
        self.assertEqual(listener.wait_for('url',.01),{'ok':False,'status':'media_load_failed','errorCode':104})
        listener=video.PlaybackEvents();listener.new_launch_error(None)
        self.assertEqual(listener.wait_for('url',.01)['status'],'receiver_launch_failed')

    def fake_cast(self,playing=True):
        cast=Mock();cast.status=SimpleNamespace(namespaces=[],display_name=None,app_id=None)
        cast.cast_info=SimpleNamespace(port=8009)
        def play(url,content_type,**_kwargs):
            listener=cast.media_controller.register_status_listener.call_args.args[0]
            if playing:listener.new_media_status(SimpleNamespace(player_state='PLAYING',content_id=url,content_type=content_type))
            else:listener.load_media_failed(None,None)
        cast.media_controller.play_media.side_effect=play
        return cast

    def test_cast_url_without_pairing_or_devtools_and_disconnects(self):
        cast=self.fake_cast();output=io.StringIO()
        with patch.object(video,'create_cast',return_value=cast) as create,patch.object(video.tv_discovery,'discover_tvs') as discover,contextlib.redirect_stdout(output):
            code=video.main(['--host','192.168.1.10','--url','https://example.com/clip.mp4?signature=FAKE_SECRET'])
        self.assertEqual(code,0)
        discover.assert_not_called()
        create.assert_called_once_with('192.168.1.10',8009,30)
        cast.media_controller.play_media.assert_called_once()
        cast.disconnect.assert_called_once()
        self.assertNotIn('FAKE_SECRET',output.getvalue())
        self.assertIn('PLAYING',output.getvalue())

    def test_status_never_launches_or_loads_media(self):
        cast=self.fake_cast()
        with patch.object(video,'create_cast',return_value=cast),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(video.main(['--host','192.168.1.10','--status']),0)
        cast.media_controller.play_media.assert_not_called()
        cast.media_controller.update_status.assert_not_called()

    def test_load_failed_not_retried(self):
        cast=self.fake_cast(False)
        with patch.object(video,'create_cast',return_value=cast),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(video.main(['--host','192.168.1.10','--url','https://example.com/clip.mp4']),1)
        cast.media_controller.play_media.assert_called_once()

    def test_discovery_failure_never_sends_cast_command(self):
        with patch.object(video.tv_discovery,'discover_tvs',return_value=([],[])),patch.object(video,'create_cast') as create,contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(video.main(['--discover','--demo']),1)
        create.assert_not_called()


    def test_demo_download_is_cached_without_repeated_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'demo.mp4'
            response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            response.read.side_effect=[b'public sample bytes',b'']
            with patch.object(video.urllib.request,'urlopen',return_value=response) as fetch:
                self.assertEqual(video.download_demo(path),path)
                self.assertEqual(video.download_demo(path),path)
            fetch.assert_called_once()
            self.assertEqual(path.read_bytes(),b'public sample bytes')
            self.assertFalse(path.with_suffix('.tmp').exists())


    def test_connection_error_does_not_print_signed_url(self):
        output=io.StringIO()
        with patch.object(video,'create_cast',side_effect=RuntimeError('FAKE_SECRET_URL')),contextlib.redirect_stderr(output):
            self.assertEqual(video.main(['--host','192.168.1.10','--url','https://example.com/clip.mp4?token=FAKE_SECRET_URL']),1)
        self.assertNotIn('FAKE_SECRET_URL',output.getvalue())
        self.assertIn('No playback request sent',output.getvalue())

    def test_stop_waits_for_media_session_and_never_launches_a_receiver(self):
        cast=self.fake_cast();cast.status.namespaces=[video.MEDIA_NAMESPACE]
        def update():
            listener=cast.media_controller.register_status_listener.call_args.args[0]
            listener.new_media_status(SimpleNamespace(player_state='PLAYING',media_session_id=1))
        def stop():
            listener=cast.media_controller.register_status_listener.call_args.args[0]
            listener.new_media_status(SimpleNamespace(player_state='IDLE',media_session_id=1))
        cast.media_controller.update_status.side_effect=update
        cast.media_controller.stop.side_effect=stop
        with patch.object(video,'create_cast',return_value=cast),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(video.main(['--host','192.168.1.10','--stop']),0)
        cast.media_controller.stop.assert_called_once()
        cast.media_controller.play_media.assert_not_called()


class MediaServerTests(unittest.TestCase):
    def test_byte_range_rules(self):
        self.assertEqual(byte_range(None,10),(0,9,False))
        self.assertEqual(byte_range('bytes=2-5',10),(2,5,True))
        self.assertEqual(byte_range('bytes=7-',10),(7,9,True))
        self.assertEqual(byte_range('bytes=-3',10),(7,9,True))
        for value in ['bytes=20-','bytes=5-2','bytes=-0','bytes=1-2,4-5','invalid']:
            with self.assertRaises(ValueError):byte_range(value,10)

    def test_server_exposes_only_selected_file_and_handles_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'clip.mp4';p.write_bytes(b'0123456789')
            secret=Path(directory)/'secret.json';secret.write_text('DO_NOT_SERVE')
            server=MediaFileServer(p,'127.0.0.1','video/mp4',bind='127.0.0.1').start()
            url=urlsplit(server.url)
            def request(path,method='GET',headers=None):
                conn=http.client.HTTPConnection(url.hostname,url.port,timeout=2)
                try:
                    conn.request(method,path,headers=headers or {})
                    response=conn.getresponse();return response.status,dict(response.getheaders()),response.read()
                finally:conn.close()
            try:
                status,headers,body=request(url.path)
                self.assertEqual((status,body),(200,b'0123456789'))
                status,headers,body=request(url.path,headers={'Range':'bytes=2-5'})
                self.assertEqual((status,body),(206,b'2345'))
                self.assertEqual(headers['Content-Range'],'bytes 2-5/10')
                self.assertEqual(headers['Access-Control-Allow-Origin'],'*')
                status,headers,body=request(url.path,'HEAD')
                self.assertEqual((status,body),(200,b''))
                self.assertEqual(headers['Content-Length'],'10')
                self.assertEqual(request(url.path,headers={'Range':'bytes=20-'})[0],416)
                for denied in ['/','/secret.json','/../secret.json','/%2e%2e/secret.json']:
                    status,headers,body=request(denied)
                    self.assertEqual(status,404)
                    self.assertNotIn(b'DO_NOT_SERVE',body)
            finally:server.close()


if __name__=='__main__':unittest.main()
