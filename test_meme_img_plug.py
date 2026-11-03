import json
import base64
import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
import meme_img_plug as meme


class MemeTests(unittest.TestCase):
    def test_local_gif_preserves_original_bytes(self):
        original = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'animation with spaces.gif'
            path.write_bytes(original)
            data_url = meme.image_file_source(path)
        self.assertTrue(data_url.startswith('data:image/gif;base64,'))
        self.assertEqual(base64.b64decode(data_url.split(',', 1)[1]), original)

    def test_local_file_type_comes_from_contents_not_extension(self):
        original = b'\x89PNG\r\n\x1a\n' + b'example'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wrong-extension.gif'
            path.write_bytes(original)
            self.assertTrue(meme.image_file_source(path).startswith('data:image/png;base64,'))
            path.write_text('not an image')
            with self.assertRaises(ValueError):
                meme.image_file_source(path)

    def test_missing_file_fails_before_network(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(meme.discovery, 'scan') as scan, \
             patch('sys.stderr'):
            with self.assertRaises(SystemExit) as error:
                meme.main(['--image-file', str(Path(directory) / 'missing.gif')])
        self.assertEqual(error.exception.code, 2)
        scan.assert_not_called()

    def test_file_and_url_are_mutually_exclusive(self):
        with patch('sys.stderr'), self.assertRaises(SystemExit):
            meme.main(['--image-file', 'image.gif', '--image-url', 'https://example.com/x.png'])

    def test_raw_gif_is_wrapped_in_correct_data_url(self):
        raw = base64.b64encode(b'GIF89a' + b'\x01\x00\x01\x00' + b'\x00\x00\x00;').decode()
        self.assertEqual(meme.normalize_image_source(raw), 'data:image/gif;base64,' + raw)
        self.assertEqual(meme.normalize_image_source('data:image/gif;base64,' + raw),
                         'data:image/gif;base64,' + raw)
        self.assertEqual(meme.normalize_image_source(raw[:8] + '\n' + raw[8:]),
                         'data:image/gif;base64,' + raw)

    def test_invalid_or_mislabeled_image_is_rejected(self):
        raw = base64.b64encode(b'GIF89a' + b'\x01\x00\x01\x00').decode()
        for invalid in ['not base64!', base64.b64encode(b'hello').decode(),
                        'data:image/png;base64,' + raw, 'data:image/gif,plain-text']:
            with self.assertRaises(ValueError):
                meme.normalize_image_source(invalid)

    def test_https_images_still_work(self):
        url = 'https://example.com/test.gif?x=1&y=2'
        self.assertEqual(meme.normalize_image_source(url), url)

    def test_cdp_correlates_reply_and_ignores_events(self):
        ws = Mock()
        ws.recv.side_effect = [json.dumps({'method': 'Runtime.consoleAPICalled'}),
                              json.dumps({'id': 1, 'result': {'result': {
                                  'value': {'ok': True, 'naturalWidth': 612}}}})]
        result = meme.evaluate(ws, 'test-expression')
        command = json.loads(ws.send.call_args.args[0])
        self.assertEqual(command['method'], 'Runtime.evaluate')
        self.assertTrue(command['params']['awaitPromise'])
        self.assertEqual(result['naturalWidth'], 612)

    def test_remote_exception_is_not_reported_as_success(self):
        ws = Mock()
        ws.recv.return_value = json.dumps({'id': 1, 'result': {
            'exceptionDetails': {'text': 'Uncaught Error'}}})
        with self.assertRaisesRegex(ValueError, 'JavaScript evaluation failed'):
            meme.evaluate(ws, 'test-expression')

    def test_target_must_belong_to_the_requested_tv(self):
        target = {'type': 'page', 'webSocketDebuggerUrl':
                  'ws://192.168.50.242:45667/devtools/page/id'}
        with patch.object(meme.discovery, 'http_get', return_value=(200, json.dumps([target]).encode())):
            with self.assertRaisesRegex(ValueError, 'does not match'):
                meme.choose_target('192.168.50.233', 45667)

    def test_ambiguous_targets_are_not_silently_modified(self):
        target = {'type': 'page', 'webSocketDebuggerUrl':
                  'ws://192.168.50.233:45667/devtools/page/id'}
        with patch.object(meme.discovery, 'http_get', return_value=(200, json.dumps([target, target]).encode())):
            with self.assertRaisesRegex(ValueError, 'Expected one page target'):
                meme.choose_target('192.168.50.233', 45667)


if __name__ == '__main__':
    unittest.main()
