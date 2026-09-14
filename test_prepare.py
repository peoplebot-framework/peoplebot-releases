"""Offline preparation safety checks. SPDX-License-Identifier: GPL-3.0-only."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import prepare

class PreparationChecks(unittest.TestCase):
    def setUp(self):
        self.request=json.loads(Path(__file__).with_name('request.example.json').read_text())
    def complete(self):
        r=copy.deepcopy(self.request)
        r['repositories']=[{'id':123,'full_name':'example-owner/example-repository'}]
        r['destination']={'kind':'papertrail_tls','host':'logs1.papertrailapp.com','port':12345}
        r['gateway_url']='https://gateway.example.test/webhooks/github/example-project'
        return r
    def test_prepare_without_destination_or_scope(self):
        p=prepare.plan(self.request)
        self.assertEqual(p['status'],'prepared_waiting_inputs')
        self.assertEqual(len(p['missing']),3);self.assertFalse(p['activation_authorized'])
    def test_complete_request_still_requires_operator(self):
        p=prepare.plan(self.complete())
        self.assertEqual(p['status'],'prepared_for_operator_review');self.assertEqual(p['missing'],[])
        self.assertFalse(p['activation_authorized'])
    def test_changed_binding_changes_request_identity(self):
        r=self.complete();first=prepare.plan(r)['request_sha256']
        r['destination']['port']=12346
        self.assertNotEqual(first,prepare.plan(r)['request_sha256'])
        reordered=dict(reversed(list(r.items())))
        self.assertEqual(prepare.plan(r)['request_sha256'],prepare.plan(reordered)['request_sha256'])
    def test_reject_credentials_extra_fields_duplicate_bindings(self):
        for key in ('secret','password','instructions','approved'):
            r=self.complete();r[key]='untrusted'
            with self.assertRaises(ValueError):prepare.plan(r)
        r=self.complete();r['repositories'].append(copy.deepcopy(r['repositories'][0]))
        with self.assertRaises(ValueError):prepare.plan(r)
    def test_reject_unsafe_endpoints(self):
        for url in ('http://gateway.example.test/webhooks/github/example-project',
                    'https://name:password@gateway.example.test/webhooks/github/example-project',
                    'https://gateway.example.test/webhooks/github/someone-else',
                    'https://gateway.example.test/webhooks/github/example-project?secret=value',
                    'https://192.0.2.1/webhooks/github/example-project'):
            r=self.complete();r['gateway_url']=url
            with self.assertRaises(ValueError):prepare.plan(r)
        for host in ('attacker.example.test','127.0.0.1','logs1.papertrailapp.com.attacker.test'):
            r=self.complete();r['destination']['host']=host
            with self.assertRaises(ValueError):prepare.plan(r)
    def test_reject_wrong_types_and_event_ambiguity(self):
        r=self.complete();r['repositories'][0]['id']=True
        with self.assertRaises(ValueError):prepare.plan(r)
        for events in ([],['*','push'],['push','push'],[123]):
            r=self.complete();r['events']=events
            with self.assertRaises(ValueError):prepare.plan(r)
    def test_cli_no_overwrite_no_secret_echo(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);request=root/'request.json';output=root/'result.json'
            request.write_text(json.dumps(self.request))
            cmd=[sys.executable,str(Path(__file__).with_name('prepare.py')),'--request',str(request),'--output',str(output)]
            p=subprocess.run(cmd,capture_output=True,text=True);self.assertEqual(p.returncode,0)
            original=output.read_bytes()
            self.assertEqual(subprocess.run(cmd,capture_output=True).returncode,2)
            self.assertEqual(output.read_bytes(),original)
            r=self.complete();r['secret']='DO_NOT_ECHO_THIS';request.write_text(json.dumps(r))
            p=subprocess.run(cmd,capture_output=True,text=True);self.assertEqual(p.returncode,2)
            self.assertNotIn('DO_NOT_ECHO_THIS',p.stdout+p.stderr)
    def test_duplicate_json_keys_and_size_bound(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'request.json'
            p.write_text('{"format":"first","format":"second"}')
            with self.assertRaises(ValueError):prepare.load(p)
            p.write_bytes(b' '*65537)
            with self.assertRaises(ValueError):prepare.load(p)

if __name__=='__main__':unittest.main()
