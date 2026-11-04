import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import vizio_talkback as talkback


class TalkBackTests(unittest.TestCase):
    def config(self,*args):
        return talkback.operation_config(talkback.parser().parse_args(args))

    def test_text_auto_enables_and_readonly_flags_reject_changes(self):
        self.assertTrue(self.config('--say','Hello')['enable'])
        for args in [('--disable','--say','Hello'),('--status','--rate','Fast'),
                     ('--options','--enable'),('--say','   ')]:
            with self.assertRaises(ValueError):self.config(*args)

    def test_utf8_file_and_size_validation_before_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'text.txt';p.write_text('Hello Joe',encoding='utf-8-sig')
            self.assertEqual(self.config('--text-file',str(p))['text'],'Hello Joe')
            p.write_bytes(b'x'*(talkback.MAX_TEXT_BYTES+1))
            with self.assertRaises(ValueError):self.config('--text-file',str(p))

    def test_cancelled_tv_menu_never_opens_debugger(self):
        tv={'host':'192.168.1.10','name':'Test TV','model':'V705-H3','api_port':7345}
        with patch.object(talkback.tv_discovery,'discover_tvs',return_value=([tv],[])),patch('builtins.input',return_value='q'),patch.object(talkback,'choose_target') as choose,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(talkback.main(['--discover','--say','Hello']),1)
        choose.assert_not_called()

    def run_js(self,config,mode='success'):
        if not shutil.which('node'):self.skipTest('Node optional for JavaScript behavior tests')
        setup='''const calls=[];const state={talkback:'Off',speech_rate:'Normal',hash:7};
const ok=(data={})=>({ok:true,status:200,json:async()=>({STATUS:{RESULT:'SUCCESS'},...data})});
globalThis.fetch=async(url,options={})=>{
 const path=new URL(url).pathname;const method=options.method||'GET';
 const payload=options.body?JSON.parse(options.body):null;
 calls.push({path,method,payload});
 if(path==='/app/get_auth_token')return ok({ITEM:{VALUE:'FAKE_TTS_AUTH'}});
 const modern='/menu_native/dynamic/tv_settings/accessibility';
 const legacy='/menu_native/dynamic/tv_settings/system/accessibility';
 if(path===modern||path===legacy){
   if(MODE==='layout_auth_failure')return {ok:false,status:403,json:async()=>({STATUS:{RESULT:'REQUIRES_PAIRING'}})};
   if(MODE==='no_layout'||(MODE==='legacy'&&path===modern))return {ok:true,status:200,json:async()=>({STATUS:{RESULT:'URI_NOT_FOUND'}})};
   return ok({ITEMS:[{CNAME:'talkback',TYPE:'T_LIST_V1'},{CNAME:'speech_rate',TYPE:'T_LIST_V1'}]});
 }
 if(MODE==='legacy'&&path.startsWith('/menu_native/')&&!path.includes('/system/accessibility/'))throw Error('wrong menu layout');
 if(path.includes('/static/')){
   const key=path.split('/').pop();return ok({ITEMS:[{CNAME:key,ELEMENTS:key==='talkback'?['Off','On']:['Slow','Normal','Fast']}]});
 }
 if(path.includes('/dynamic/')){
   const key=path.split('/').pop();
   if(method==='PUT'){
     if(MODE==='cleanup_failure'&&key==='talkback'&&payload.VALUE==='Off')return {ok:false,status:200,json:async()=>({STATUS:{RESULT:'FAILURE'}})};
     if(MODE==='write_failure')return {ok:false,status:200,json:async()=>({STATUS:{RESULT:'READ_ONLY_ERROR'}})};
     if(payload.HASHVAL!==state.hash)throw Error('stale hash');
     state[key]=payload.VALUE;state.hash++;
   }
   return ok({ITEMS:[{CNAME:key,TYPE:'T_LIST_V1',VALUE:state[key],HASHVAL:state.hash}]});
 }
 if(path==='/app/tts/enabled')return ok({ITEM:{VALUE:state.talkback==='On'?1:0}});
 if(path==='/app/tts/play'){
   if(MODE==='lost_reply')throw Error('connection lost');
   return ok();
 }
 throw Error('unexpected route');
};'''.replace('MODE',json.dumps(mode))
        setup="const waits=[];const realSetTimeout=globalThis.setTimeout;globalThis.setTimeout=(callback,ms,...args)=>{if(ms>=1000){waits.push(ms);return realSetTimeout(callback,0,...args);}return realSetTimeout(callback,ms,...args);};\n"+setup
        expression=talkback.talkback_expression(config,.5)
        script=setup+'Promise.resolve(eval('+json.dumps(expression)+')).then(result=>console.log(JSON.stringify({result,calls,state,waits,injected:!!globalThis.injected})));'
        return json.loads(subprocess.check_output(['node'],input=script,text=True,timeout=5))

    def test_status_is_readonly_and_exposes_verified_rates(self):
        out=self.run_js(self.config('--status'))
        self.assertTrue(out['result']['ok'])
        self.assertEqual(out['result']['supportedRates'],['Slow','Normal','Fast'])
        self.assertFalse(any(c['method']=='PUT' for c in out['calls']))
        self.assertEqual(out['result']['voiceSelection'],'not_exposed')

    def test_enable_uses_real_setting_and_fresh_hash_before_speech(self):
        out=self.run_js(self.config('--rate','fast','--say','Hello Joe'))
        self.assertTrue(out['result']['ok'])
        self.assertEqual(out['state']['talkback'],'On')
        self.assertEqual(out['state']['speech_rate'],'Fast')
        writes=[c for c in out['calls'] if c['method']=='PUT']
        self.assertEqual([c['path'].split('/')[-1] for c in writes],['speech_rate','talkback','play'])
        self.assertEqual(writes[0]['payload'],{'REQUEST':'MODIFY','HASHVAL':7,'VALUE':'Fast'})
        self.assertEqual(writes[1]['payload']['HASHVAL'],8)
        self.assertEqual(writes[2]['payload'],{'VALUE':'Hello Joe'})
        self.assertNotIn('Hello Joe',json.dumps(out['result']))
        self.assertNotIn('FAKE_TTS_AUTH',json.dumps(out['result']))

    def test_invalid_rate_sends_no_writes(self):
        out=self.run_js(self.config('--rate','turbo','--say','Hello'))
        self.assertEqual(out['result']['status'],'unsupported_rate')
        self.assertFalse(any(c['method']=='PUT' for c in out['calls']))

    def test_setting_write_rejected_prevents_speech(self):
        out=self.run_js(self.config('--say','Hello'),'write_failure')
        self.assertEqual(out['result']['apiResult'],'READ_ONLY_ERROR')
        self.assertFalse(any(c['path']=='/app/tts/play' for c in out['calls']))

    def test_lost_speech_reply_reports_unknown_and_preserves_changes(self):
        out=self.run_js(self.config('--say','Hello'),'lost_reply')
        self.assertEqual(out['result']['outcome'],'unknown')
        self.assertEqual(out['result']['stage'],'speak')
        self.assertEqual(out['result']['changes'],{'talkback':'On'})

    def test_text_is_data_and_placeholder_words_are_preserved(self):
        text='TIME_LIMIT OPERATION ");globalThis.injected=true;//'
        out=self.run_js(self.config('--say',text))
        self.assertFalse(out['injected'])
        self.assertEqual(out['calls'][-1]['payload']['VALUE'],text)

    def test_stop_uses_library_space_payload_without_enabling(self):
        out=self.run_js(self.config('--stop'))
        writes=[c for c in out['calls'] if c['method']=='PUT']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0]['payload'],{'VALUE':' '})
        self.assertEqual(out['state']['talkback'],'Off')


    def test_auto_disable_requires_text_and_valid_delay(self):
        for args in [('--auto-disable',),('--disable-after','10'),
                     ('--say','Hello','--disable-after','0'),
                     ('--say','Hello','--disable-after','nan')]:
            with self.assertRaises(ValueError):self.config(*args)
        config=self.config('--say','Hello','--disable-after','10')
        self.assertTrue(config['autoDisable'])
        self.assertEqual(talkback.cleanup_wait_budget(config),10)

    def test_fixed_delay_finishes_with_native_talkback_off(self):
        out=self.run_js(self.config('--say','Hello Joe','--disable-after','10'))
        self.assertTrue(out['result']['ok'])
        self.assertEqual(out['waits'],[10000])
        self.assertEqual(out['state']['talkback'],'Off')
        self.assertTrue(out['result']['autoDisabled'])
        self.assertFalse(out['result']['nativeSpeechEnabled'])
        self.assertFalse(out['result']['speechCompletionVerified'])
        self.assertEqual(out['result']['completionMethod'],'fixed_delay')
        writes=[c for c in out['calls'] if c['method']=='PUT']
        self.assertEqual([c['path'].split('/')[-1] for c in writes],['talkback','play','talkback'])
        self.assertEqual(writes[-1]['payload']['HASHVAL'],8)
        self.assertEqual(sum(c['path']=='/app/get_auth_token' for c in out['calls']),2)

    def test_auto_delay_is_estimated_and_budget_covers_slowest_rate(self):
        config=self.config('--say','A slightly longer test sentence with several words.','--auto-disable','--rate','Slow')
        out=self.run_js(config)
        delay=out['result']['disableAfterSeconds']
        self.assertGreaterEqual(delay,8)
        self.assertGreaterEqual(talkback.cleanup_wait_budget(config),delay)
        self.assertEqual(out['result']['completionMethod'],'estimated_delay')
        self.assertEqual(out['state']['talkback'],'Off')

    def test_lost_speech_reply_still_attempts_to_disable(self):
        out=self.run_js(self.config('--say','Hello','--auto-disable'),'lost_reply')
        self.assertFalse(out['result']['ok'])
        self.assertEqual(out['result']['outcome'],'unknown')
        self.assertTrue(out['result']['autoDisabled'])
        self.assertEqual(out['state']['talkback'],'Off')
        self.assertFalse(out['waits'])

    def test_failed_cleanup_is_reported_without_false_success(self):
        out=self.run_js(self.config('--say','Hello','--auto-disable'),'cleanup_failure')
        self.assertFalse(out['result']['ok'])
        self.assertTrue(out['result']['cleanupFailed'])
        self.assertFalse(out['result']['autoDisabled'])
        self.assertEqual(out['state']['talkback'],'On')



    def test_legacy_layout_is_used_for_options_writes_and_auto_disable(self):
        out=self.run_js(self.config('--say','Hello Joe','--auto-disable'),'legacy')
        self.assertTrue(out['result']['ok'])
        self.assertTrue(out['result']['autoDisabled'])
        self.assertEqual(out['state']['talkback'],'Off')
        menu_calls=[c for c in out['calls'] if c['path'].startswith('/menu_native/')]
        self.assertEqual(menu_calls[0]['path'],'/menu_native/dynamic/tv_settings/accessibility')
        self.assertTrue(all('/system/accessibility' in c['path'] for c in menu_calls[1:]))
        self.assertTrue(any(c['method']=='PUT' for c in menu_calls))

    def test_layout_discovery_does_not_hide_authorization_failure(self):
        out=self.run_js(self.config('--say','Hello'),'layout_auth_failure')
        self.assertFalse(out['result']['ok'])
        self.assertEqual(out['result']['apiResult'],'REQUIRES_PAIRING')
        self.assertFalse(any('/system/accessibility' in c['path'] for c in out['calls']))
        self.assertFalse(any(c['method']=='PUT' for c in out['calls']))

    def test_missing_both_layouts_makes_no_changes(self):
        out=self.run_js(self.config('--say','Hello'),'no_layout')
        self.assertEqual(out['result']['status'],'accessibility_menu_not_found')
        self.assertFalse(any(c['method']=='PUT' for c in out['calls']))


if __name__=='__main__':unittest.main()
