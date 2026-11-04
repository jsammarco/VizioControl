import json
import shutil
import subprocess
import unittest
from unittest.mock import Mock

import launch_app_devtools as launch


class LaunchTests(unittest.TestCase):
    def config(self, *args):
        return launch.launch_config(launch.parser().parse_args(args))

    def test_namespace_and_app_id_are_both_required(self):
        for args in [('--app-id', '3'), ('--namespace', '2'), ()]:
            with self.assertRaises(ValueError):
                self.config(*args)
        self.assertEqual(self.config('--namespace', '2', '--app-id', '3'),
                         {'NAME_SPACE': 2, 'APP_ID': '3', 'MESSAGE': None})

    def test_presets_preserve_namespace_hex_ids_and_message(self):
        netflix = self.config('--app', 'Netflix')
        youtube = self.config('--app', 'YouTubeTV')
        self.assertEqual((netflix['NAME_SPACE'], netflix['APP_ID']), (3, '1'))
        self.assertEqual((youtube['NAME_SPACE'], youtube['APP_ID']), (5, '1'))
        pluto = self.config('--app', 'pluto tv')
        self.assertEqual(pluto['APP_ID'], 'E6F74C01')
        self.assertEqual(pluto['NAME_SPACE'], 0)
        self.assertEqual(json.loads(pluto['MESSAGE'])['CAST_NAMESPACE'], 'urn:x-cast:tv.pluto')

    def test_duplicate_ids_keep_distinct_messages(self):
        toon = self.config('--app', 'Toon Goggles')
        vudu = self.config('--app', 'Vudu')
        self.assertEqual(toon['APP_ID'], vudu['APP_ID'])
        self.assertIsNone(toon['MESSAGE'])
        self.assertTrue(vudu['MESSAGE'].startswith('https://'))

    def test_validation_and_message_override(self):
        self.assertEqual(self.config('--app', 'Hulu', '--message', 'custom')['MESSAGE'], 'custom')
        for args in [('--namespace', '-1', '--app-id', '3'),
                     ('--namespace', '2', '--app-id', ''),
                     ('--app', 'unknown'), ('--app', 'Hulu', '--namespace', '2')]:
            with self.assertRaises(ValueError):
                self.config(*args)

    def test_cdp_ignores_events_and_unrelated_responses(self):
        ws = Mock()
        ws.recv.side_effect = [json.dumps({'method': 'Runtime.consoleAPICalled'}),
                              json.dumps({'id': 9}), json.dumps({'id': 2, 'result': {
                                  'result': {'value': {'ok': True, 'status': 'accepted'}}}})]
        self.assertEqual(launch.evaluate(ws, 'expression', 2, 1)['status'], 'accepted')
        params = json.loads(ws.send.call_args.args[0])['params']
        self.assertTrue(params['awaitPromise'])

    @unittest.skipUnless(shutil.which('node'), 'Node is optional for JavaScript behavior tests')
    def test_javascript_exact_arguments_and_no_input_execution(self):
        message = 'TIMEOUT CONFIG ");globalThis.injected=true;//'
        config = {'NAME_SPACE': 0, 'APP_ID': 'E6F74C01', 'MESSAGE': message}
        expression = launch.launch_expression(config, .05)
        script = 'globalThis.VIZIO={launchApp(...args){globalThis.received=args.slice(0,3);args[3]("SUCCESS");}};'
        script += 'Promise.resolve(eval(' + json.dumps(expression) + ')).then(result=>console.log(JSON.stringify({result,args:globalThis.received,injected:!!globalThis.injected})));'
        data = json.loads(subprocess.check_output(['node'], input=script, text=True, timeout=5))
        self.assertEqual(data['result'], {'ok': True, 'status': 'accepted'})
        self.assertEqual(data['args'], [0, 'E6F74C01', message])
        self.assertFalse(data['injected'])

    @unittest.skipUnless(shutil.which('node'), 'Node is optional for JavaScript behavior tests')
    def test_javascript_failure_timeout_and_missing_bridge(self):
        config = {'NAME_SPACE': 2, 'APP_ID': '3', 'MESSAGE': None}
        for setup, expected in [('globalThis.VIZIO={launchApp(n,i,m,cb){cb("FAILURE")}};', 'rejected'),
                                ('globalThis.VIZIO={launchApp(){}};', 'timeout'),
                                ('globalThis.VIZIO={};', 'unsupported')]:
            expression = launch.launch_expression(config, .02)
            script = setup + 'Promise.resolve(eval(' + json.dumps(expression) + ')).then(r=>console.log(JSON.stringify(r)));'
            result = json.loads(subprocess.check_output(['node'], input=script, text=True, timeout=5))
            self.assertFalse(result['ok'])
            self.assertEqual(result['status'], expected)


if __name__ == '__main__':
    unittest.main()
