import copy
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from agent.schema import Case, Labels, Judgment, Score, DIMENSIONS, Review, Finding
from agent.rubric import calculate
from agent.evidence import inspect_evidence, validate_judgment
from agent.engine import run_case, safe_case
from agent.judge import judge, JudgeError
from agent.storage import Store
from server import make_handler, csv_safe
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / 'examples/cases.json').read_text())


def all_scores(level=5, source_id='web', quote='本轮课程申请截止日期为2026-10-28。'):
    return Judgment(scores={i: Score(level=level, reason='依据正文核对', source_ids=[source_id] if i in {'accuracy','citations'} else [], source_quote=quote if i in {'accuracy','citations'} else '') for i in DIMENSIONS}, summary='核对完成')


class EvidenceTests(unittest.TestCase):
    def test_all_examples_validate(self):
        for data in CASES:
            Case.model_validate(data)

    def test_conflict_not_resolved_by_channel_or_recency(self):
        case=Case.model_validate(CASES[0])
        ev=inspect_evidence(case)
        self.assertEqual(len(ev['conflicts']),1)
        self.assertEqual(ev['claims'][0]['verdict'],'conflict')
        self.assertTrue(ev['holds'])

    def test_explicit_replacement_only(self):
        case=Case.model_validate(CASES[1])
        ev=inspect_evidence(case)
        self.assertEqual(ev['active_source_ids'],['correction'])
        self.assertEqual(ev['source_status']['web'],'superseded')
        self.assertEqual(ev['claims'][0]['verdict'],'contradicted')

    def test_later_title_alone_not_replacement(self):
        case=Case.model_validate(CASES[0])
        case.sources[1].published_at=date(2026,10,26)
        self.assertEqual(len(inspect_evidence(case)['conflicts']),1)

    def test_expired_single_event_has_no_valid_evidence(self):
        ev=inspect_evidence(Case.model_validate(CASES[2]))
        self.assertEqual(ev['source_status']['expired'],'expired')
        self.assertFalse(ev['active_source_ids'])

    def test_historical_answer_evaluated_at_answer_date(self):
        case=Case.model_validate(CASES[2]);case.answered_at=date(2026,10,19)
        self.assertEqual(inspect_evidence(case)['source_status']['expired'],'usable')

    def test_long_term_old_date_does_not_expire_document(self):
        case=Case.model_validate(CASES[3]);case.sources[0].valid_until=date(2024,10,1)
        self.assertEqual(inspect_evidence(case)['source_status']['guide'],'usable')

    def test_future_correction_not_used_for_old_answer(self):
        case=Case.model_validate(CASES[1]);case.sources[1].published_at=date(2026,10,28)
        ev=inspect_evidence(case)
        self.assertEqual(ev['active_source_ids'],['web'])
        self.assertEqual(ev['source_status']['correction'],'future_publication')

    def test_no_evidence_is_not_pass(self):
        run=run_case(Case.model_validate(CASES[5]))
        self.assertIsNone(run['decision']['final_total'])
        self.assertEqual(run['decision']['label'],'待核实')

    def test_unknown_answer_date_not_assumed_today(self):
        case=Case.model_validate(CASES[0]);case.answered_at=None
        self.assertFalse(inspect_evidence(case)['active_source_ids'])

    def test_campus_mismatch_excluded(self):
        case=Case.model_validate(CASES[3]);case.audience.campus=['江湾校区'];case.sources[0].labels.campus=['邯郸校区']
        self.assertEqual(inspect_evidence(case)['source_status']['guide'],'out_of_scope')

    def test_address_does_not_automatically_narrow_campus(self):
        case=Case.model_validate(CASES[3]);case.sources[0].text+=' 收件地址：邯郸校区。'
        self.assertEqual(inspect_evidence(case)['source_status']['guide'],'usable')

    def test_exclusive_labels(self):
        for field,values in [('campus',['全校通用','江湾校区']),('identity',['通用','医学生']),('level',['未明确','硕士生']),('scene',['其他','学业指导'])]:
            with self.assertRaises(ValidationError):Labels(**{field:values})

    def test_invalid_fact_quotes_rejected(self):
        data=copy.deepcopy(CASES[0]);data['sources'][0]['facts'][0]['quote']='不存在'
        with self.assertRaises(ValidationError):Case.model_validate(data)

    def test_answer_edit_invalidates_annotations(self):
        data=copy.deepcopy(CASES[0]);data['answer']='新答案'
        with self.assertRaises(ValidationError):Case.model_validate(data)

    def test_boundary_candidate(self):
        self.assertTrue(any(s['code']=='boundary_candidate' for s in run_case(Case.model_validate(CASES[6]))['signals']))

    def test_privacy_redacted_before_storage(self):
        case=Case.model_validate(CASES[5]);case.answer='联系私人手机号13912345678'
        run=run_case(case)
        self.assertNotIn('13912345678',json.dumps(run,ensure_ascii=False))
        self.assertTrue(any(s['code']=='privacy_candidate' for s in run['signals']))

    def test_prompt_injection_cannot_assign_score(self):
        run=run_case(Case.model_validate(CASES[7]))
        self.assertIsNone(run['decision']['final_total'])
        self.assertTrue(all(v['level'] is None for v in run['judgment']['scores'].values()))


class ScoringTests(unittest.TestCase):
    def test_unknown_audience_not_assumed_applicable(self):
        case=Case.model_validate(CASES[3]);case.audience.level=['未明确']
        self.assertIn('用户适用条件未知，需核对限定范围',inspect_evidence(case)['holds'])

    def test_confirmed_model_risk_requires_answer_quote(self):
        case=Case.model_validate(CASES[3]);j=all_scores(source_id='guide',quote=case.sources[0].text)
        j.findings=[Finding(code='privacy_leak',status='confirmed',reason='疑似泄露')]
        with self.assertRaises(ValueError):validate_judgment(j,case,inspect_evidence(case))

    def test_direct_block_does_not_display_full_score(self):
        d=calculate(all_scores(),['privacy_leak'])
        self.assertEqual(d['raw_total'],100)
        self.assertIsNone(d['final_total'])
        self.assertEqual(d['label'],'不通过')

    def test_weights_total_100(self):
        self.assertEqual(calculate(all_scores())['final_total'],100)

    def test_exact_weighted_score(self):
        j=all_scores();j.scores['accuracy'].level=3
        self.assertEqual(calculate(j)['final_total'],90)

    def test_unknown_not_zero_or_renormalized(self):
        j=all_scores();j.scores['accuracy'].level=None
        d=calculate(j)
        self.assertIsNone(d['final_total']);self.assertEqual(d['known_subtotal'],75)
        self.assertNotEqual(d['label'],'通过')

    def test_caps_dominate(self):
        d=calculate(all_scores(),['fabrication','latest_without_source'])
        self.assertEqual(d['final_total'],59);self.assertEqual(d['grade'],'D')

    def test_privacy_and_danger_block_even_missing_scores(self):
        for risk in ['privacy_leak','dangerous_steps']:
            j=all_scores(None);d=calculate(j,[risk])
            self.assertTrue(d['blocked']);self.assertEqual(d['label'],'不通过')

    def test_conflict_blocks_high_total(self):
        self.assertEqual(calculate(all_scores(),holds=['来源冲突'])['label'],'待核实')

    def test_invalid_levels_dimensions_rejected(self):
        with self.assertRaises(ValidationError):Score(level=4,reason='x')
        with self.assertRaises(ValidationError):Judgment(scores={},summary='x')

    def test_nonexistent_source_quote_rejected(self):
        c=Case.model_validate(CASES[0]);j=all_scores(quote='编造引用')
        with self.assertRaises(ValueError):validate_judgment(j,c,inspect_evidence(c))

    def test_expired_source_cannot_support_score(self):
        c=Case.model_validate(CASES[2]);j=all_scores(source_id='expired')
        with self.assertRaises(ValueError):validate_judgment(j,c,inspect_evidence(c))

    def test_uncited_factual_score_rejected(self):
        c=Case.model_validate(CASES[0]);j=all_scores();j.scores['accuracy'].source_quote=''
        with self.assertRaises(ValueError):validate_judgment(j,c,inspect_evidence(c))

    def test_csv_formula_escaped(self):
        for s in ['=1+1',' +cmd','@sum(1)','\t=1']:
            self.assertTrue(csv_safe(s).startswith("'"))
        self.assertEqual(csv_safe('普通文字'),'普通文字')


class APIAndModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.store=Store(Path(cls.temp.name)/'test.sqlite')
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(cls.store))
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    def request(self,path,body=None,headers=None):
        request=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or {'Content-Type':'application/json'})
        with urllib.request.urlopen(request) as response:return json.load(response)

    def test_actual_http_run_review_export_and_immutability(self):
        run=self.request('/api/run',{'case':CASES[3],'mode':'rules'})
        original=self.store.get_run(run['run_id'])
        source=run['case']['sources'][0]
        j=all_scores(source_id=source['id'],quote=source['text'])
        review={'run_id':run['run_id'],'reviewer':'tester','judgment':j.model_dump(),'resolution':'已解决','reason':'逐条核对正文与必要要点，三步齐全','confirmed_risks':[]}
        saved=self.request('/api/review',review)
        self.assertEqual(saved['decision']['final_total'],100)
        self.assertEqual(self.store.get_run(run['run_id']),original)
        exported=self.request('/api/export')
        self.assertTrue(any(x['id']==saved['id'] for x in exported['reviews']))
        # 重开Store验证持久化。
        self.assertEqual(Store(self.store.path).get_run(run['run_id']),original)

    def test_missing_model_configuration_returns_failure_not_score(self):
        with patch.dict(os.environ,{'JUDGE_BASE_URL':'','JUDGE_MODEL':''}):
            with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/api/run',{'case':CASES[3],'mode':'llm'})
        self.assertEqual(exc.exception.code,502)

    def test_cross_origin_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/api/run',{}, {'Content-Type':'application/json','Origin':'https://evil.invalid'})
        self.assertEqual(exc.exception.code,403)

    def test_dns_rebinding_host_blocked(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/api/bootstrap',headers={'Host':'evil.invalid'})
        self.assertEqual(exc.exception.code,403)

    def test_private_source_files_not_served(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/.runtime/quality.sqlite')
        self.assertEqual(exc.exception.code,404)

    def test_import_invalid_reference_rejected_atomically(self):
        data=copy.deepcopy(CASES[0]);data['sources'][0]['facts'][0]['quote']='missing'
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request('/api/import',{'cases':[CASES[3],data]})
        self.assertEqual(exc.exception.code,400)

    def test_mock_model_valid_json_and_usage(self):
        c=Case.model_validate(CASES[3]);ev=inspect_evidence(c)
        j=all_scores(source_id='guide',quote=c.sources[0].text)
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return json.dumps({'choices':[{'finish_reason':'stop','message':{'content':j.model_dump_json()}}],'usage':{'total_tokens':123}}).encode()
        with patch.dict(os.environ,{'JUDGE_BASE_URL':'http://localhost:9999/v1','JUDGE_MODEL':'test'}),patch('urllib.request.OpenerDirector.open',return_value=Response()):
            result,meta=judge(c,ev)
        self.assertEqual(result.scores['accuracy'].level,5);self.assertEqual(meta['usage']['total_tokens'],123)

    def test_mock_model_truncation_empty_and_invalid_json(self):
        for finish,content in [('length','{}'),('stop',''),('stop','not json'),('stop','{}')]:
            class Response:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def read(self,*args):return json.dumps({'choices':[{'finish_reason':finish,'message':{'content':content}}]}).encode()
            with patch.dict(os.environ,{'JUDGE_BASE_URL':'http://localhost:9999/v1','JUDGE_MODEL':'test'}),patch('urllib.request.OpenerDirector.open',return_value=Response()):
                with self.assertRaises(JudgeError):judge(Case.model_validate(CASES[3]),{})

    def test_endpoint_plain_http_external_disallowed(self):
        with patch.dict(os.environ,{'JUDGE_BASE_URL':'http://remote.invalid/v1','JUDGE_MODEL':'test'}):
            with self.assertRaises(JudgeError):judge(Case.model_validate(CASES[3]),{})


if __name__=='__main__':unittest.main()
