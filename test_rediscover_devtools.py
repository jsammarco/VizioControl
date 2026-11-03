import argparse
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch
import rediscover_devtools as discovery


class DiscoveryTests(unittest.TestCase):
    def test_port_range_validation(self):
        self.assertEqual(discovery.port_range('1-65535'), (1, 65535))
        self.assertEqual(discovery.port_range('33012'), (33012, 33012))
        for bad in ('0', '65536', '2-1', '1-2-3', 'abc'):
            with self.assertRaises(argparse.ArgumentTypeError):
                discovery.port_range(bad)

    def test_target_urls_drop_query_credentials(self):
        data = [{'title': 'Paramount+', 'type': 'page',
                 'url': 'https://example.com/app?token=private#secret',
                 'webSocketDebuggerUrl': 'ws://192.168.50.233:33012/devtools/page/123'}]
        result = discovery.clean_targets(data)
        self.assertEqual(result[0]['url'], 'https://example.com/app')
        self.assertNotIn('private', str(result))

    def test_catchall_response_is_not_a_debugger(self):
        with patch.object(discovery, 'http_get', return_value=(200, b'status=ok')):
            self.assertNotIn('targets', discovery.find_targets('192.168.50.233', 9080))
        self.assertEqual(discovery.clean_targets({'status': 'ok'}), [])
        self.assertEqual(discovery.clean_targets([{'title': 'not a target'}]), [])

    def test_probe_only_requests_target_metadata(self):
        with patch.object(discovery, 'http_get', return_value=(200,
                b'[{"title":"Home","webSocketDebuggerUrl":"ws://tv/devtools/page/id"}]')) as get:
            result = discovery.find_targets('192.168.50.233', 38713)
        get.assert_called_once_with('192.168.50.233', 38713, '/json/list', timeout=2)
        self.assertEqual(result['debugger_page'], 'http://192.168.50.233:38713/')

    def test_cached_hit_does_not_scan_the_network(self):
        hit = {'port': 51013, 'targets': [{'title': 'Home'}],
               'debugger_page': 'http://192.168.50.233:51013/'}
        with patch.object(discovery, 'find_targets', return_value=hit), \
             patch.object(discovery, 'connect_port') as connect, \
             patch.object(discovery, 'log'):
            result = discovery.scan('192.168.50.233', 1, 65535, 2, .2, preferred=[51013])
        connect.assert_not_called()
        self.assertFalse(result['scan_complete'])
        self.assertEqual(result['ports_scanned'], 1)
        self.assertEqual(result['debuggers'], [hit])

    def test_new_debugger_stops_early(self):
        def targets(host, port):
            return {'port': port, 'targets': [{}], 'debugger_page': 'http://tv/'}
        with patch.object(discovery, 'connect_port', side_effect=lambda h,p,t: p if p == 3 else None), \
             patch.object(discovery, 'find_targets', side_effect=targets), \
             patch.object(discovery, 'log'):
            result = discovery.scan('192.168.50.233', 1, 1000, 2, .2)
        self.assertTrue(result['stopped_after_debugger'])
        self.assertLess(result['ports_scanned'], 1000)
        self.assertEqual(result['debuggers'][0]['port'], 3)

    def test_full_scan_continues_after_cached_hit(self):
        hit = {'port': 3, 'targets': [{}], 'debugger_page': 'http://tv/'}
        with patch.object(discovery, 'find_targets', return_value=hit), \
             patch.object(discovery, 'connect_port', return_value=None), \
             patch.object(discovery, 'log'):
            result = discovery.scan('192.168.50.233', 1, 16, 2, .2, full=True, preferred=[3])
        self.assertTrue(result['scan_complete'])
        self.assertEqual(result['ports_scanned'], 16)

    def test_priority_and_cache_are_host_specific(self):
        from itertools import islice
        first = list(islice(discovery.ordered_ports(1, 65535, [51013, 51013]), 3))
        self.assertEqual(first, [51013, 32768, 32769])
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'ports.json'
            discovery.remember_ports('192.168.50.233', [51013], cache)
            discovery.remember_ports('192.168.50.233', [40000, 51013], cache)
            self.assertEqual(discovery.cached_ports('192.168.50.233', cache), [40000, 51013])
            self.assertEqual(discovery.cached_ports('192.168.50.234', cache), [])

    def test_watch_searches_again_after_disconnect_and_opens_new_port(self):
        hits = [{'port': port, 'targets': [{}], 'debugger_page': f'http://tv:{port}/'}
                for port in (40001, 40002)]

        def fake_scan(*args, **kwargs):
            hit = hits.pop(0)
            kwargs['on_found'](hit)
            return {'open_tcp_ports': [hit['port']], 'debuggers': [hit]}

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(discovery, 'scan', side_effect=fake_scan) as scan, \
             patch.object(discovery, 'find_targets', return_value={'error': 'disconnected'}), \
             patch.object(discovery, 'cached_ports', return_value=[]), \
             patch.object(discovery, 'remember_ports'), \
             patch.object(discovery, 'replay', return_value=[]) as replay, \
             patch.object(discovery.time, 'sleep', side_effect=[None, KeyboardInterrupt()]), \
             patch.object(discovery.webbrowser, 'open') as open_page, \
             patch.object(discovery, 'log'):
            result = discovery.main(['--watch', '--replay', '--open', '--output',
                                     str(Path(directory) / 'watch.json')])
        self.assertEqual(result, 130)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(replay.call_count, 2)
        self.assertEqual([call.args[0] for call in open_page.call_args_list],
                         ['http://tv:40001/', 'http://tv:40002/'])


if __name__ == '__main__':
    unittest.main()
