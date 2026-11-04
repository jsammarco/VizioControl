import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from cdp_key_session import KeySession,KEY_URL
import paired_remote
import remote_devtools as remote


class KeySessionTests(unittest.TestCase):
    def test_cors_adapter_preserves_server_error_and_resumes_cdp(self):
        ws=Mock()
        ws.recv.side_effect=[json.dumps({'method':'Fetch.requestPaused','params':{
            'requestId':'request','request':{'url':KEY_URL},'responseStatusCode':500,
            'responseHeaders':[{'name':'Content-Type','value':'text/html'}]}}),
            json.dumps({'id':2,'result':{}}),json.dumps({'id':1,'result':{'value':'done'}})]
        session=KeySession(ws)
        self.assertEqual(session.call('test',{}),{'value':'done'})
        commands=[json.loads(c.args[0]) for c in ws.send.call_args_list]
        continued=commands[1]
        self.assertEqual(continued['method'],'Fetch.continueResponse')
        self.assertEqual(continued['params']['responseCode'],500)
        headers={h['name']:h['value'] for h in continued['params']['responseHeaders']}
        self.assertEqual(headers['Access-Control-Allow-Origin'],'*')
        self.assertEqual(headers['Access-Control-Max-Age'],'0')

    def test_unrelated_url_is_not_given_cors_headers(self):
        session=KeySession(Mock())
        session.continue_response({'requestId':'other','request':{'url':'https://example.com/'},'responseStatusCode':200})
        command=json.loads(session.ws.send.call_args.args[0])
        self.assertEqual(command['method'],'Fetch.continueRequest')
        self.assertEqual(command['params'],{'requestId':'other'})

    def test_only_key_endpoint_is_intercepted(self):
        session=KeySession(Mock())
        with patch.object(session,'call') as call:
            session.enable()
        self.assertEqual(call.call_args.args[1],{'patterns':[{'urlPattern':KEY_URL,'requestStage':'Response'}]})


class PairedRemoteTests(unittest.TestCase):
    def config(self):
        return {'host':'192.168.1.10','port':7345,'device_id':'test-id','device_name':'Test Remote'}

    def test_decline_default_no_and_eof_make_no_requests(self):
        for read in [lambda prompt:'',lambda prompt:'n',Mock(side_effect=EOFError)]:
            with patch.object(paired_remote,'TV') as tv,patch.object(paired_remote,'save') as save:
                with self.assertRaises(paired_remote.PairingDeclined):
                    paired_remote.pair_if_needed(Path('unused'),self.config(),input_fn=read)
                tv.assert_not_called();save.assert_not_called()

    def test_existing_pairing_does_not_request_new_pairing(self):
        config={**self.config(),'auth_token':'FAKE_PAIR_TOKEN'}
        read=Mock()
        with patch.object(paired_remote,'TV') as tv:
            paired_remote.pair_if_needed(Path('unused'),config,input_fn=read)
        read.assert_not_called();tv.return_value.request.assert_not_called()

    def test_yes_then_hidden_code_pairs_without_printing_credentials(self):
        output=io.StringIO();config=self.config();code=Mock(return_value='1234')
        with patch.object(paired_remote,'TV') as tv,patch.object(paired_remote,'save') as save,contextlib.redirect_stdout(output):
            tv.return_value.request.side_effect=[{'ITEM':{'PAIRING_REQ_TOKEN':'FAKE_CHALLENGE','CHALLENGE_TYPE':1}},
                                                  {'ITEM':{'AUTH_TOKEN':'FAKE_PAIR_TOKEN'}}]
            paired_remote.pair_if_needed(Path('unused'),config,input_fn=lambda prompt:'yes',code_fn=code)
        requests=tv.return_value.request.call_args_list
        self.assertEqual([c.args[0] for c in requests],['/pairing/start','/pairing/pair'])
        self.assertEqual(requests[1].args[1]['RESPONSE_VALUE'],'1234')
        self.assertEqual(config['auth_token'],'FAKE_PAIR_TOKEN')
        self.assertNotIn('FAKE_',output.getvalue());self.assertNotIn('1234',output.getvalue())
        self.assertNotIn('pairing',config)

    def test_cancel_after_code_request_cancels_challenge_without_buttons(self):
        with patch.object(paired_remote,'TV') as tv,patch.object(paired_remote,'save'),contextlib.redirect_stdout(io.StringIO()):
            tv.return_value.request.side_effect=[{'ITEM':{'PAIRING_REQ_TOKEN':'fake','CHALLENGE_TYPE':1}},{}]
            with self.assertRaises(paired_remote.PairingDeclined):
                paired_remote.pair_if_needed(Path('unused'),self.config(),input_fn=lambda prompt:'y',code_fn=lambda prompt:'q')
        self.assertEqual([c.args[0] for c in tv.return_value.request.call_args_list],['/pairing/start','/pairing/cancel'])

    def test_config_cannot_reuse_different_tv_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'remote.json';path.write_text(json.dumps(self.config()))
            with self.assertRaises(paired_remote.TVError):paired_remote.load_config('192.168.1.20',path)

    def test_paired_button_request_contains_keylist_and_auth_flag(self):
        tv=Mock();keys=[{'CODESET':5,'CODE':4,'ACTION':'KEYPRESS'}]
        self.assertTrue(paired_remote.send_keys(tv,keys)['ok'])
        tv.request.assert_called_once_with('/key_command/',{'KEYLIST':keys},paired=True)

    def test_paired_check_without_credential_never_prompts_or_requests(self):
        with patch.object(paired_remote,'load_config',return_value=(Path('unused'),self.config())),patch.object(paired_remote,'pair_if_needed') as pair,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(remote.main(['--host','192.168.1.10','--transport','paired','--check']),1)
        pair.assert_not_called()

    def test_cli_pair_decline_never_uses_devtools_or_sends_buttons(self):
        with patch.object(paired_remote,'load_config',return_value=(Path('unused'),self.config())),patch.object(paired_remote,'TV') as tv,patch.object(remote,'choose_target') as target,patch('builtins.input',return_value=''),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(remote.main(['--host','192.168.1.10','--transport','paired','--button','mute-toggle']),1)
        tv.assert_not_called();target.assert_not_called()


if __name__=='__main__':unittest.main()
