import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'tools'))
import novel_state as ns
import online_state as online
import private_book as vault


class PrivateBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        for name in ('project_config.json', 'system_config.json', 'platforms.json'):
            shutil.copyfile(SOURCE / name, self.project / name)
        cfg = online.get_config(self.project)
        cfg['authority']['repository_url'] = ''
        ns.atomic_json(self.project / 'project_config.json', cfg)
        # 这些用例验证旧版存储/迁移，不测试新书发布前检查。
        legacy = ns.read_json(self.project / 'system_config.json')
        legacy['serial_mode'] = False
        ns.atomic_json(self.project / 'system_config.json', legacy)
        online.bootstrap(self.project, 'https://github.com/example/synthetic')
        for kind in ('canon', 'roadmap'):
            file = self.base / (kind + '.md')
            file.write_text('仅供加密存储测试的合成设定', encoding='utf-8')
            state = ns.load_state(self.project / 'book')
            ns.set_input(self.project / 'book', kind, file, '合成测试', state['revision'])
        wf = online.load_workflow(self.project)
        wf['selected_topic'] = {'id': 'SYNTHETIC'}
        wf['platform'] = 'qimao'
        ns.atomic_json(self.project / 'book/workflow.json', wf)
        self.work = self.base / 'private-book'
        shutil.copytree(self.project / 'book', self.work)
        self.key = self.base / 'key.json'
        vault.make_key(self.key, ns.load_state(self.work)['book_id'])
        self.text = '这只是合成正文，不能充作任何正式小说。'
        text_file = self.base / 'chapter.txt'
        text_file.write_text(self.text, encoding='utf-8')
        # 测试使用短合成章，不改实际作品的严格字数配置。
        state = ns.load_state(self.work)
        state['config']['strict_length'] = False
        ns.atomic_json(self.work / 'state.json', state)
        ns.candidate(self.work, 1, text_file)
        record = dict(chapter=1, title='合成测试', stage='S01', base_revision=state['revision'],
                      canon_sha256=state['inputs']['canon']['sha256'], text_sha256=ns.digest(self.text),
                      complete=True, story_time_end=1, summary='测试记录，不是创作产物。',
                      review=dict(read_full_text=True, decision='adopt', blocking_issues=[], sources_checked=[], conclusion='仅检验存储。'),
                      facts=[], progression={k: '合成测试' for k in ('goal','obstacle','method','choice','result','consequence','next_options','scene_function')}, thread_events=[])
        file = self.base / 'record.json'
        ns.atomic_json(file, record)
        ns.adopt(self.work, text_file, file)

    def tearDown(self):
        self.tmp.cleanup()

    def seal(self):
        return vault.pack(self.project, self.work, self.key)

    def test_roundtrip_preserves_actual_adopted_files_and_history(self):
        result = self.seal()
        out = self.base / 'restored'
        vault.unlock(self.project, self.key, out)
        self.assertEqual(ns.load_state(out), ns.load_state(self.work))
        item = ns.load_state(out)['chapters']['1']
        self.assertEqual((out / item['text_path']).read_text(), self.text)
        self.assertEqual(result['chapters'], 1)
        self.assertNotIn(self.text, (self.project / result['path']).read_text())

    def test_serial_freeze_survives_legacy_storage_roundtrip(self):
        ns.serial_module().enable(self.work, '合成迁移测试', ns.load_state(self.work)['revision'], ns.serial_api())
        self.seal()
        out = self.base / 'serial-restored'
        vault.unlock(self.project, self.key, out)
        self.assertEqual(ns.status(out)['frozen_chapters'], 1)
        self.assertEqual(ns.status(out)['published_chapters'], 0)

    def test_public_baseline_cannot_be_mistaken_for_current_state(self):
        self.seal()
        with self.assertRaisesRegex(ns.WorkspaceError, '私密稿'):
            ns.load_state(self.project / 'book')

    def test_index_uses_private_progress_without_reading_plaintext(self):
        self.seal()
        index = online.build_index(self.project)
        self.assertEqual(index['progress']['adopted_chapters'], 1)
        self.assertEqual(index['chapters'][0]['storage'], 'encrypted_book_v1')
        self.assertNotIn(self.text, json.dumps(index, ensure_ascii=False))
        self.assertTrue(online.validate_index(self.project)['valid'])
        online.set_task(self.project, 'WRITING', '等待下一章', '下一章', 2)

    def test_wrong_key_and_overwrite_restore_are_rejected(self):
        self.seal()
        wrong = self.base / 'wrong.json'
        vault.make_key(wrong, ns.load_state(self.work)['book_id'])
        with self.assertRaises(ns.WorkspaceError):
            vault.unlock(self.project, wrong, self.base / 'wrong-out')
        out = self.base / 'existing'
        out.mkdir()
        with self.assertRaises(ns.WorkspaceError):
            vault.unlock(self.project, self.key, out)

    def test_cipher_tampering_is_detected(self):
        result = self.seal()
        file = self.project / result['path']
        file.write_text(file.read_text().replace('ciphertext_base64', 'ciphertext_base65'))
        with self.assertRaisesRegex(ns.WorkspaceError, '哈希'):
            vault.read_public(self.project)

    def test_header_tampering_fails_authentication_even_after_public_hash_recomputed(self):
        result = self.seal()
        file = self.project / result['path']
        value = ns.read_json(file)
        value['public']['title'] = '被篡改的标题'
        encoded = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
        path = 'vault/book_' + ns.digest(encoded) + '.json'
        (self.project / path).write_text(encoded)
        pointer = ns.read_json(self.project / 'book/private_storage.json')
        pointer.update(path=path, sha256=ns.digest(encoded))
        ns.atomic_json(self.project / 'book/private_storage.json', pointer)
        with self.assertRaisesRegex(ns.WorkspaceError, '认证'):
            vault.unlock(self.project, self.key, self.base / 'tampered')

    def test_stale_working_copy_cannot_replace_newer_vault(self):
        self.seal()
        old = self.base / 'old'
        new = self.base / 'new'
        vault.unlock(self.project, self.key, old)
        vault.unlock(self.project, self.key, new)
        vault.pack(self.project, new, self.key)
        with self.assertRaisesRegex(ns.WorkspaceError, '当前加密稿'):
            vault.pack(self.project, old, self.key)

    def test_paths_and_key_replacement_are_rejected(self):
        for path in ('../leak', '/absolute', 'a//b', 'a/./b', 'a\\b'):
            self.assertFalse(vault.valid_relative(path))
        with self.assertRaises(ns.WorkspaceError):
            vault.make_key(self.key, ns.load_state(self.work)['book_id'])

    def test_authorized_plain_migration_preserves_files_history_and_live_workflow(self):
        self.seal()
        online.set_task(self.project, 'WRITING', '合成的最新任务', '继续合成测试', 2)
        workflow = online.load_workflow(self.project)
        before = ns.load_state(self.work)
        result = vault.publish_plain(self.project, self.key, '合成授权：取消阅读密钥并直接读取。')
        self.assertFalse(result['requires_reader_key'])
        self.assertFalse((self.project / 'book/private_storage.json').exists())
        self.assertFalse((self.project / 'book/private_parent.json').exists())
        self.assertEqual(ns.load_state(self.project / 'book'), before)
        self.assertEqual(online.load_workflow(self.project), workflow)
        for folder in ('chapters', 'records', 'candidates', 'inputs'):
            for source in (self.work / folder).glob('*'):
                self.assertEqual((self.project / 'book' / folder / source.name).read_bytes(), source.read_bytes())
        index = online.build_index(self.project)
        self.assertEqual(index['source']['manuscript_storage'], 'plain_files_v1')
        self.assertFalse(index['source']['requires_reader_key'])
        self.assertNotIn('storage', index['chapters'][0])
        self.assertTrue(online.validate_index(self.project)['valid'])

    def test_plain_migration_wrong_key_leaves_official_pointer_in_place(self):
        self.seal()
        pointer = (self.project / 'book/private_storage.json').read_bytes()
        wrong = self.base / 'another-key.json'
        vault.make_key(wrong, ns.load_state(self.work)['book_id'])
        with self.assertRaises(ns.WorkspaceError):
            vault.publish_plain(self.project, wrong, '合成授权')
        self.assertEqual((self.project / 'book/private_storage.json').read_bytes(), pointer)

    def test_plain_migration_rejects_immutable_conflict_without_changing_state(self):
        self.seal()
        before = (self.project / 'book/state.json').read_bytes()
        item = ns.load_state(self.work)['chapters']['1']
        file = self.project / 'book' / item['text_path']
        file.parent.mkdir(parents=True)
        file.write_text('这也是合成内容，但与正式采用不符。')
        with self.assertRaisesRegex(ns.WorkspaceError, '冲突'):
            vault.publish_plain(self.project, self.key, '合成授权')
        self.assertTrue((self.project / 'book/private_storage.json').exists())
        self.assertEqual((self.project / 'book/state.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
