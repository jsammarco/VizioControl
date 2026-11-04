import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import remote_devtools as remote


class RemoteTests(unittest.TestCase):
    def config(self, *args):
        return remote.key_config(remote.parser().parse_args(args))

    def test_named_buttons_match_tv_codes_and_preserve_order(self):
        keys=self.config('--button','volume-up','mute-toggle','channel-down','previous-channel','power-off')
        self.assertEqual([(k['CODESET'],k['CODE']) for k in keys],[(5,1),(5,4),(8,0),(8,2),(11,0)])
        self.assertTrue(all(k['ACTION']=='KEYPRESS' for k in keys))

    def test_raw_keydown_and_keyup(self):
        for action in ['KEYDOWN','KEYUP','KEYPRESS']:
            self.assertEqual(self.config('--codeset','5','--code','1','--action',action),
                             [{'CODESET':5,'CODE':1,'ACTION':action}])
        for args in [('--codeset','5'),('--code','1'),('--codeset','-1','--code','0')]:
            with self.assertRaises(ValueError):self.config(*args)

    def test_sequence_keeps_actions_and_rejects_extra_fields(self):
        keys=[{'CODESET':5,'CODE':1,'ACTION':'KEYDOWN'},{'CODESET':5,'CODE':1,'ACTION':'KEYUP'}]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'keys.json'
            path.write_text(json.dumps({'KEYLIST':keys}))
            self.assertEqual(self.config('--sequence-file',str(path)),keys)
            with self.assertRaises(ValueError):self.config('--sequence-file',str(path),'--action','KEYUP')
        for invalid in [[],[{'CODESET':True,'CODE':0,'ACTION':'KEYPRESS'}],keys+[{'url':'other'}]]:
            with self.assertRaises(ValueError):remote.validate_keys(invalid)

    def test_check_requires_no_buttons_and_list_is_offline(self):
        self.assertEqual(self.config('--check'),[])
        with patch.object(remote,'choose_target') as choose, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(remote.main(['--list-buttons']),0)
        choose.assert_not_called()

    def test_menu_cancel_never_connects_to_debugger(self):
        devices=[{'host':'192.168.1.10','name':'Test TV','model':'V705-H3','api_port':7345}]
        with patch.object(remote.tv_discovery,'discover_tvs',return_value=(devices,[])), patch('builtins.input',return_value='q'), patch.object(remote,'choose_target') as choose, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(remote.main(['--discover','--button','volume-up']),1)
        choose.assert_not_called()

    def run_js(self, check=False, mode='success'):
        if not shutil.which('node'):
            self.skipTest('Node optional for JavaScript behavior tests')
        expression=remote.key_expression([{'CODESET':5,'CODE':1,'ACTION':'KEYPRESS'}],.03,check)
        # Simulated page-local token is intentionally fake. No real TV is contacted.
        setup='''const calls=[]; globalThis.fetch=async(url,options={})=>{
 calls.push({url,method:options.method||'GET',body:options.body,auth:options.headers?.AUTH});
 if(url.endsWith('/app/get_auth_token')) return {ok:true,status:200,json:async()=>(AUTH_REPLY)};
 SECOND_REPLY
};'''
        auth={'STATUS':{'RESULT':'SUCCESS'},'ITEM':{'VALUE':'FAKE_LOCAL_ONLY_TOKEN'}}
        if mode=='auth_failure':auth={'STATUS':{'RESULT':'FAILURE'}}
        second="return {ok:true,status:200,json:async()=>({STATUS:{RESULT:'SUCCESS'}})};"
        if mode=='rejected':second="return {ok:false,status:403,json:async()=>({STATUS:{RESULT:'FAILURE'}})};"
        if mode=='server_error':second="return {ok:false,status:500,json:async()=>{throw Error('non-JSON error page')}};"
        if mode=='timeout':second="return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Error('abort'))));"
        setup=setup.replace('AUTH_REPLY',json.dumps(auth)).replace('SECOND_REPLY',second)
        script=setup+'Promise.resolve(eval('+json.dumps(expression)+')).then(result=>console.log(JSON.stringify({result,calls})));'
        return json.loads(subprocess.check_output(['node'],input=script,text=True,timeout=5))

    def test_javascript_sends_exact_keylist_and_keeps_token_out_of_result(self):
        output=self.run_js()
        self.assertEqual(output['result']['status'],'accepted')
        calls=output['calls'];self.assertEqual(len(calls),2)
        self.assertEqual(calls[1]['method'],'PUT')
        self.assertEqual(calls[1]['auth'],'FAKE_LOCAL_ONLY_TOKEN')
        self.assertEqual(json.loads(calls[1]['body']),{'KEYLIST':[{'CODESET':5,'CODE':1,'ACTION':'KEYPRESS'}]})
        self.assertNotIn('FAKE_LOCAL_ONLY_TOKEN',json.dumps(output['result']))
        self.assertTrue(all('power_mode' not in c['url'] for c in calls))

    def test_check_probes_key_route_with_zero_buttons_and_auth_failure_never_writes(self):
        output=self.run_js(True)
        self.assertEqual(len(output['calls']),2)
        self.assertEqual(json.loads(output['calls'][1]['body']),{'KEYLIST':[]})
        self.assertEqual(output['result']['keysSent'],0)
        failed=self.run_js(mode='auth_failure')
        self.assertEqual(len(failed['calls']),1)
        self.assertFalse(failed['result']['ok'])

    def test_server_error_is_not_reported_as_successful_check(self):
        output=self.run_js(True,'server_error')
        self.assertFalse(output['result']['ok'])
        self.assertEqual(output['result']['httpStatus'],500)
        self.assertEqual(output['result']['outcome'],'not_sent')
        actual=self.run_js(False,'server_error')['result']
        self.assertEqual(actual['outcome'],'unknown')
        self.assertNotIn('keysSent',actual)


    def test_rejection_and_interrupted_write_have_distinct_outcomes(self):
        rejected=self.run_js(mode='rejected')['result']
        self.assertEqual(rejected['status'],'rejected')
        interrupted=self.run_js(mode='timeout')['result']
        self.assertEqual(interrupted['outcome'],'unknown')
        self.assertEqual(interrupted['status'],'request_interrupted')


if __name__ == '__main__':
    unittest.main()
