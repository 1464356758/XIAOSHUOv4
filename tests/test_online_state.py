import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

SOURCE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SOURCE/'tools'))
import online_state as online
import make_init_package as pack


class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.project=Path(self.tmp.name)/'project';self.project.mkdir()
        for name in ('project_config.json','system_config.json','platforms.json'):
            shutil.copyfile(SOURCE/name,self.project/name)
        config=online.get_config(self.project);config['authority']['repository_url']=''
        online.ns.atomic_json(self.project/'project_config.json',config)
        online.bootstrap(self.project,'https://github.com/example/novel')

    def tearDown(self):self.tmp.cleanup()

    def jsonfile(self,name,value):
        path=Path(self.tmp.name)/name;path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');return path

    def research(self,partial=False):
        online.select_platform(self.project,'qidian')
        data={'platform':'qidian','checked_at':'2026-10-22T10:00:00+08:00','coverage_status':'partial' if partial else 'verified','sources':[{'id':'S1','title':'官方测试来源','url':'https://www.qidian.com/rank/','accessed_at':'2026-10-22T10:00:00+08:00'}],'rules':[{'topic':'AI辅助要求','status':'unknown'}],'rank_samples':[] if partial else [{'title':f'测试样本{i}','author':'测试作者','list_name':'测试榜','observed_at':'2026-10-22T10:00:00+08:00','source_id':'S1','book_url':'https://www.qidian.com/'} for i in range(5)],'gaps':['人工构造的测试数据，不是实际扫榜']}
        path=self.jsonfile('research.json',data);online.save_research(self.project,path);return data

    def topics(self,count=8):
        return {'candidates':[dict(id=f'T{i+1:02d}',title=f'原创测试方向{i+1}',genre='测试',pitch='测试卖点',reader_promise='测试承诺',long_form_engine='测试动力',stage_variation='测试变化',risk='测试风险') for i in range(count)]}

    def test_bootstrap_is_idempotent(self):
        before=online.ns.load_state(self.project/'book');online.bootstrap(self.project,'https://github.com/example/novel');after=online.ns.load_state(self.project/'book')
        self.assertEqual(before,after)

    def test_different_repository_cannot_reuse_book_identity(self):
        with self.assertRaises(online.ns.WorkspaceError):online.bootstrap(self.project,'https://github.com/example/other')

    def test_new_workspaces_have_different_identities(self):
        p=Path(self.tmp.name)/'second';p.mkdir()
        for name in ('project_config.json','system_config.json','platforms.json'):shutil.copyfile(SOURCE/name,p/name)
        config=online.get_config(p);config['authority']['repository_url']='';online.ns.atomic_json(p/'project_config.json',config)
        online.bootstrap(p,'https://github.com/example/second')
        self.assertNotEqual(online.ns.load_state(p/'book')['book_id'],online.ns.load_state(self.project/'book')['book_id'])

    def test_first_state_waits_for_platform(self):
        self.assertEqual(online.load_workflow(self.project)['phase'],'WAITING_PLATFORM')
        self.assertEqual(online.build_index(self.project)['chapters'],[])

    def test_cannot_start_writing_without_topic_and_planning(self):
        with self.assertRaises(online.ns.WorkspaceError):online.set_task(self.project,'WRITING','写作','检查')

    def test_cannot_fabricate_completion(self):
        with self.assertRaises(online.ns.WorkspaceError):online.set_task(self.project,'COMPLETE','完稿','导出')

    def test_stale_reader_index_is_detected(self):
        workflow=online.load_workflow(self.project);workflow['detail']='新任务';online.ns.atomic_json(self.project/'book/workflow.json',workflow)
        with self.assertRaises(online.ns.WorkspaceError):online.validate_index(self.project)
        online.build_index(self.project);self.assertTrue(online.validate_index(self.project)['valid'])

    def test_research_rejects_nonofficial_sources(self):
        data=self.research(partial=True);data['sources'][0]['url']='https://example.org/blog'
        with self.assertRaises(online.ns.WorkspaceError):online.save_research(self.project,self.jsonfile('bad.json',data))

    def test_complete_ranking_requires_at_least_five_samples(self):
        data=self.research(partial=True);data['coverage_status']='verified'
        with self.assertRaises(online.ns.WorkspaceError):online.save_research(self.project,self.jsonfile('bad.json',data))

    def test_partial_research_remains_explicit(self):
        self.research(partial=True);self.assertEqual(online.build_index(self.project)['research']['coverage_status'],'partial')

    def test_topic_count_is_five_to_ten(self):
        self.research()
        for count in (4,11):
            with self.assertRaises(online.ns.WorkspaceError):online.save_topics(self.project,self.jsonfile('topics.json',self.topics(count)))

    def test_topics_wait_for_author_and_do_not_start_prose(self):
        self.research();online.save_topics(self.project,self.jsonfile('topics.json',self.topics()))
        self.assertEqual(online.load_workflow(self.project)['phase'],'WAITING_TOPIC')
        self.assertIsNone(online.load_workflow(self.project)['selected_topic']);self.assertEqual(online.ns.load_state(self.project/'book')['chapters'],{})

    def test_selection_requires_an_actual_candidate(self):
        self.research();online.save_topics(self.project,self.jsonfile('topics.json',self.topics()))
        with self.assertRaises(online.ns.WorkspaceError):online.select_topic(self.project,'T99','作者选择')
        online.select_topic(self.project,'T01','作者选择T01')
        self.assertEqual(online.load_workflow(self.project)['phase'],'PLANNING')
        self.assertEqual(online.ns.load_state(self.project/'book')['title'],'原创测试方向1')

    def test_duplicate_topic_ids_are_rejected(self):
        self.research();data=self.topics();data['candidates'][1]['id']='T01'
        with self.assertRaises(online.ns.WorkspaceError):online.save_topics(self.project,self.jsonfile('topics.json',data))

    def test_reader_manifest_matches_source_state_hash(self):
        data=online.build_index(self.project)
        self.assertEqual(data['source']['state_sha256'],online.ns.digest((self.project/'book/state.json').read_text()))
        self.assertTrue(online.validate_index(self.project)['valid'])

    def test_empty_template_excludes_live_identity_and_research(self):
        output=Path(self.tmp.name)/'init.zip';pack.build(SOURCE,output)
        with zipfile.ZipFile(output) as archive:
            names=archive.namelist();self.assertNotIn('book/state.json',names);self.assertNotIn('book/workflow.json',names)
            self.assertFalse(any(x.startswith('book/chapters/') or x.startswith('book/records/') for x in names))
            config=json.loads(archive.read('project_config.json'));self.assertEqual(config['authority']['repository_url'],'')
            manifest=json.loads(archive.read('online/index.json'));self.assertFalse(manifest['initialized']);self.assertIsNone(manifest['book']['id'])
            self.assertEqual(manifest['topics'],[])

    def test_v42_template_bootstraps_two_independent_serial_books(self):
        output=Path(self.tmp.name)/'serial-init.zip';pack.build(SOURCE,output)
        identities=[]
        for number in (1,2):
            target=Path(self.tmp.name)/f'fresh-{number}'
            with zipfile.ZipFile(output) as archive:
                for required in ('tools/serial_release.py','每日一章指令.txt','docs/连载冻结与发布前检查.md'):
                    self.assertIn(required,archive.namelist())
                archive.extractall(target)
            run=subprocess.run([sys.executable,'tools/online_state.py','bootstrap','--repo',f'https://github.com/example/fresh-{number}'],cwd=target,capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)
            state=json.loads((target/'book/state.json').read_text())
            identities.append(state['book_id'])
            self.assertTrue(state['config']['serial_mode'])
            self.assertEqual(state['serialization']['frozen'],{})
            self.assertEqual(state['serialization']['published'],{})
            self.assertEqual(json.loads((target/'book/workflow.json').read_text())['phase'],'WAITING_PLATFORM')
            self.assertEqual(json.loads((target/'online/index.json').read_text())['progress']['actual_han'],0)
        self.assertNotEqual(identities[0],identities[1])

    def test_bad_repository_url_is_rejected(self):
        for url in ('https://github.com.evil.test/a/b','https://github.com/a/b/tree/main','https://user@github.com/a/b'):
            with self.assertRaises(online.ns.WorkspaceError):online.repository(url)

    def test_init_archive_can_bootstrap_as_an_independent_new_book(self):
        output=Path(self.tmp.name)/'fresh.zip';pack.build(SOURCE,output)
        fresh=Path(self.tmp.name)/'fresh'
        with zipfile.ZipFile(output) as archive:archive.extractall(fresh)
        result=subprocess.run([sys.executable,'tools/online_state.py','bootstrap','--repo','https://github.com/example/fresh-book'],cwd=fresh,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        info=json.loads(result.stdout);self.assertEqual(info['phase'],'WAITING_PLATFORM')
        self.assertNotEqual(info['book_id'],online.ns.load_state(self.project/'book')['book_id'])
        check=subprocess.run([sys.executable,'tools/online_state.py','validate-index'],cwd=fresh,capture_output=True,text=True)
        self.assertEqual(check.returncode,0,check.stderr)


if __name__=='__main__':unittest.main()
