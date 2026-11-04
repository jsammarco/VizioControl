import contextlib
import io
import json
import unittest
from unittest.mock import MagicMock, patch

import discover_tvs as discovery
import launch_app_devtools as launch

TVS = [
    {'host':'192.168.1.10','name':'Living Room','model':'V705-H3','api_port':7345},
    {'host':'192.168.1.20','name':'Bedroom','model':'V505-G9','api_port':7345},
]


class DiscoveryTests(unittest.TestCase):
    def test_ssdp_sends_kinoma_query_and_deduplicates_responder(self):
        sock = MagicMock()
        sock.recvfrom.side_effect = [(b'HTTP/1.1 200 OK\r\nLOCATION: http://untrusted.invalid/\r\n', ('192.168.1.10',1900)),
                                     (b'HTTP/1.1 200 OK\r\n', ('192.168.1.10',1900))]
        with patch.object(discovery.socket, 'socket') as make_socket, patch.object(discovery.time, 'monotonic', side_effect=[0,0,0,4]):
            make_socket.return_value.__enter__.return_value = sock
            hosts = discovery.ssdp_candidates('192.168.1.5',3)
        self.assertEqual(hosts, {'192.168.1.10'})
        requests = [c.args[0] for c in sock.sendto.call_args_list]
        self.assertTrue(any(b'ST: urn:schemas-kinoma-com:device:shell:1\r\n' in r for r in requests))
        self.assertTrue(all(b'MAN: "ssdp:discover"\r\nMX: 1\r\n' in r for r in requests))

    def test_candidates_are_validated_and_deduplicated(self):
        def probe(host):
            return dict(TVS[0]) if host == TVS[0]['host'] else None
        with patch.object(discovery,'saved_hosts',return_value={'192.168.1.10','192.168.1.99'}), patch.object(discovery,'ssdp_candidates',return_value={'192.168.1.10'}), patch.object(discovery,'probe_tv',side_effect=probe) as probe_mock:
            devices,warnings=discovery.discover_tvs()
        self.assertEqual(len(devices),1)
        self.assertEqual(devices[0]['discovered_by'], ['SSDP','saved address'])
        self.assertEqual(probe_mock.call_count,2)
        self.assertFalse(warnings)

    def test_subnet_limits_checked_before_network_access(self):
        for subnet in ['0.0.0.0/0','192.168.0.0/16','8.8.8.0/24','::1/128']:
            with patch.object(discovery,'ssdp_candidates') as ssdp:
                with self.assertRaises(ValueError):discovery.discover_tvs(subnet=subnet)
                ssdp.assert_not_called()
        self.assertEqual(str(discovery.scan_network('192.168.1.5/24')),'192.168.1.0/24')

    def test_ssdp_failure_can_still_find_validated_saved_tv(self):
        with patch.object(discovery,'saved_hosts',return_value={TVS[0]['host']}), patch.object(discovery,'ssdp_candidates',side_effect=OSError), patch.object(discovery,'probe_tv',return_value=dict(TVS[0])):
            devices,warnings=discovery.discover_tvs()
        self.assertEqual(len(devices),1)
        self.assertTrue(warnings)

    def test_menu_uses_explicit_selection_and_reprompts(self):
        answers=iter(['0','nonsense','2'])
        with contextlib.redirect_stdout(io.StringIO()):
            selected=discovery.select_tv(TVS,lambda prompt:next(answers))
        self.assertEqual(selected['host'], TVS[1]['host'])

    def test_cancel_or_eof_never_selects_a_default_tv(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for fn in [lambda prompt:'q', MagicMock(side_effect=EOFError)]:
                with self.assertRaises(discovery.SelectionCancelled):discovery.select_tv(TVS,fn)
            with self.assertRaises(ValueError):discovery.select_tv([])

    def test_probe_does_not_accept_non_vizio_json(self):
        body=json.dumps({'STATUS':{'RESULT':'SUCCESS'},'ITEMS':[{'NAME':'Other device','VALUE':{'MODEL_NAME':'Other'}}]}).encode()
        with patch.object(discovery.socket,'create_connection'), patch.object(discovery,'http_get',return_value=(200,body)):
            self.assertIsNone(discovery.probe_tv('192.168.1.10'))

    def test_launcher_list_tvs_does_not_connect_to_debugger(self):
        with patch.object(discovery,'discover_tvs',return_value=(TVS,[])), patch.object(launch,'choose_target') as target, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launch.main(['--list-tvs']),0)
        target.assert_not_called()

    def test_launcher_cancel_sends_no_debugger_command(self):
        with patch.object(discovery,'discover_tvs',return_value=(TVS,[])), patch('builtins.input',return_value='q'), patch.object(launch,'choose_target') as target, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launch.main(['--discover','--app','Hulu']),1)
        target.assert_not_called()


if __name__ == '__main__':
    unittest.main()
