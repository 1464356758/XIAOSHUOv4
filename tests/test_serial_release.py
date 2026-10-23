"""临时合成文本验证连载边界；不为真实小说自动生成通过结论。"""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import zipfile

from test_novel_state import WorkspaceFixture, ns


class SerialTests(WorkspaceFixture):
    def setUp(self):
        super().setUp()
        self.serial = ns.serial_module(); self.a = ns.serial_api()
        self.serial.enable(self.root, "测试启用连载", ns.load_state(self.root)["revision"], self.a)

    def prepare(self, n, text="甲把钥匙交给乙。", record=None):
        record = record or self.record(n, text)
        path = Path(self.temp.name) / "draft.txt"; path.write_text(text, encoding="utf-8")
        recpath = Path(self.temp.name) / "record.json"; ns.atomic_json(recpath, record)
        ns.candidate(self.root, n, path)
        state = ns.load_state(self.root)
        report = self.serial.skeleton(self.root, state, text, record, self.a)
        ev = {"chapter": n, "sha256": ns.digest(text), "quote": text[:120]}
        report.update(decision="release", read_full_text=True, blocking_issues=[],
                      reader_reason_to_continue="合成文本机制测试", consequence="测试归属改变", ending_runway="测试保留收束篇幅")
        for check in report["checks"].values():
            check.update(decision="pass", conclusion="合成测试证据", evidence=[dict(ev)])
        for item in report["comparison"]:
            body = ns.inside(self.root, state["chapters"][str(item["chapter"])]["text_path"]).read_text()
            item.update(quote=body[:120], difference="合成测试用例，不评价语义")
        for item in report["future_window"]:
            item.update(goal="测试目标", change="测试后果", ending_question="测试待办")
        for item in report["promise_reviews"]:
            item.update(deadline_chapter=10, reason="测试到期", evidence=dict(ev))
        if n == 1:
            for cue in report["opening"].values(): cue.update(quote=text.split('。')[0] + '。', why="测试引文存在")
        if "checkpoint" in report:
            report["checkpoint"].update(read_mode="full_text_batches", decision="continue",
                                        **{key: "合成测试回看结论" for key in self.serial.CONCLUSIONS})
        return path, recpath, record, report

    def release(self, n, text="甲把钥匙交给乙。", record=None):
        path, recpath, record, report = self.prepare(n, text, record)
        self.gate(path, recpath, report)
        return ns.adopt(self.root, path, recpath)

    def gate(self, path, recpath, report):
        out = Path(self.temp.name) / "release.json"; ns.atomic_json(out, report)
        return self.serial.check(self.root, path, recpath, out, self.a)

    def test_adoption_requires_actual_completed_preflight(self):
        path, recpath, rec, report = self.prepare(1)
        with self.assertRaises(ns.WorkspaceError): ns.adopt(self.root, path, recpath)
        report["decision"] = "pending"
        with self.assertRaises(ns.WorkspaceError): self.gate(path, recpath, report)
        self.assertEqual(ns.status(self.root)["adopted_chapters"], 0)

    def test_valid_release_freezes_both_versions_without_claiming_publication(self):
        self.release(1)
        state = ns.load_state(self.root)
        self.assertEqual(state["serialization"]["frozen"]["1"]["record_sha256"], state["chapters"]["1"]["record_sha256"])
        self.assertEqual(ns.status(self.root)["published_chapters"], 0)
        self.assertEqual(ns.status(self.root)["frozen_chapters"], 1)

    def test_frozen_edit_revalidate_and_canon_change_rejected(self):
        self.release(1)
        path = Path(self.temp.name) / "edit.txt"; path.write_text("测试修改")
        with self.assertRaises(ns.WorkspaceError): ns.candidate(self.root, 1, path, "edit", "测试请求")
        rec = self.record(1); report = Path(self.temp.name) / "edit.json"; ns.atomic_json(report, rec)
        with self.assertRaises(ns.WorkspaceError): ns.adopt(self.root, path, report, True)
        with self.assertRaises(ns.WorkspaceError): ns.set_input(self.root, "canon", path, "测试", ns.load_state(self.root)["revision"])

    def test_pin_rejects_state_repoint_even_if_new_file_itself_matches(self):
        self.release(1); state = ns.load_state(self.root)
        old = state["chapters"]["1"]["record_sha256"]
        state["chapters"]["1"]["record_sha256"] = "0" * 64
        with self.assertRaises(ns.WorkspaceError): self.serial.verify_frozen(state, self.a)
        self.assertNotEqual(old, "0" * 64)

    def test_gate_stale_after_roadmap_change(self):
        path, recpath, rec, report = self.prepare(1); self.gate(path, recpath, report)
        source = Path(self.temp.name) / "future.md"; source.write_text("只修改未来的测试路线")
        ns.set_input(self.root, "roadmap", source, "测试调整", ns.load_state(self.root)["revision"])
        rec["base_revision"] = ns.load_state(self.root)["revision"]; ns.atomic_json(recpath, rec)
        with self.assertRaises(ns.WorkspaceError): ns.adopt(self.root, path, recpath)

    def test_gate_stale_after_semantic_record_changed(self):
        path, recpath, rec, report = self.prepare(1); self.gate(path, recpath, report)
        rec["summary"] = "测试记录有新解释"; ns.atomic_json(recpath, rec)
        with self.assertRaises(ns.WorkspaceError): ns.adopt(self.root, path, recpath)

    def test_repair_round_trip_does_not_reuse_old_gate(self):
        path, recpath, rec, report = self.prepare(1); self.gate(path, recpath, report)
        path.write_text("乙把钥匙留下。")
        ns.candidate(self.root, 1, path, request="测试修订", repairing=True)
        path.write_text("甲把钥匙交给乙。")
        ns.candidate(self.root, 1, path, request="测试恢复", repairing=True)
        with self.assertRaises(ns.WorkspaceError): ns.adopt(self.root, path, recpath)

    def test_first_chapter_delayed_hook_rejected_by_measured_position(self):
        path, recpath, rec, report = self.prepare(1, "甲乙丙丁。有人求救。")
        state = ns.load_state(self.root); state["config"]["opening_hook_max_han"] = 4
        ns.atomic_json(self.root / "state.json", state)
        report["opening"]["first_hook"]["quote"] = "有人求救。"
        with self.assertRaises(ns.WorkspaceError): self.gate(path, recpath, report)

    def test_fifth_chapter_needs_full_review_before_adoption(self):
        for n in range(1, 5): self.release(n)
        path, recpath, rec, report = self.prepare(5)
        incomplete = copy.deepcopy(report); incomplete["checkpoint"]["coverage"].pop("5")
        with self.assertRaises(ns.WorkspaceError): self.gate(path, recpath, incomplete)
        self.gate(path, recpath, report); ns.adopt(self.root, path, recpath)
        self.assertEqual(ns.status(self.root)["reviewed_until"], 5)
        self.assertFalse(ns.status(self.root)["window_review_due"])

    def test_tenth_stage_review_also_happens_before_freeze(self):
        for n in range(1, 10): self.release(n)
        path, recpath, rec, report = self.prepare(10)
        self.assertEqual(report["checkpoint"]["kind"], "stage")
        broken = copy.deepcopy(report); broken["checkpoint"]["record_coverage"]["1"] = "bad"
        with self.assertRaises(ns.WorkspaceError): self.gate(path, recpath, broken)
        self.gate(path, recpath, report); ns.adopt(self.root, path, recpath)
        self.assertEqual(ns.status(self.root)["reviewed_until"], 10)

    def test_promise_partial_progress_cannot_reset_deadline(self):
        rec = self.record(1); rec["thread_events"] = [{"id": "TEST", "action": "plant", "description": "测试线索", "quote": "甲把钥匙交给乙。"}]
        path, recpath, rec, report = self.prepare(1, record=rec)
        report["promise_reviews"][0]["deadline_chapter"] = 2
        self.gate(path, recpath, report); ns.adopt(self.root, path, recpath)
        path, recpath, rec, report = self.prepare(2)
        with self.assertRaises(ns.WorkspaceError): self.gate(path, recpath, report)
        review = report["promise_reviews"][0]; review.update(action="miss", cost="测试失约有成本")
        self.gate(path, recpath, report); ns.adopt(self.root, path, recpath)
        self.assertEqual(ns.load_state(self.root)["serialization"]["promises"]["TEST"]["history"][0]["deadline_chapter"], 2)

    def test_old_error_remains_open_until_future_prose_supports_resolution(self):
        self.release(1); state = ns.load_state(self.root)
        report = {"id": "ERR-TEST", "base_revision": state["revision"], "problem": "测试旧错", "impact": "测试影响",
                  "forward_plan": "仅在未来承担后果", "target_chapter": 2,
                  "source": {"chapter": 1, "sha256": state["chapters"]["1"]["text_sha256"], "quote": "甲把钥匙交给乙。"}}
        file = Path(self.temp.name) / "incident.json"; ns.atomic_json(file, report)
        self.serial.incident(self.root, file, self.a)
        path, recpath, rec, release = self.prepare(2)
        with self.assertRaises(ns.WorkspaceError): self.gate(path, recpath, release)
        release["incident_actions"] = [{"id": "ERR-TEST", "action": "resolve", "explanation": "合成前向处置证据", "evidence": {"chapter": 2, "sha256": rec["text_sha256"], "quote": "甲把钥匙交给乙。"}}]
        self.gate(path, recpath, release); ns.adopt(self.root, path, recpath)
        self.assertEqual(ns.status(self.root)["open_incidents"], 0)
        self.assertEqual(ns.load_state(self.root)["serialization"]["frozen"]["1"], state["serialization"]["frozen"]["1"])

    def test_publication_needs_exact_frozen_version_and_real_past_time(self):
        self.release(1); sha = ns.load_state(self.root)["chapters"]["1"]["text_sha256"]
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        with self.assertRaises(ns.WorkspaceError): self.serial.publish(self.root, 1, "bad", past, "测试实际回执", self.a)
        with self.assertRaises(ns.WorkspaceError): self.serial.publish(self.root, 1, sha, future, "测试预约", self.a)
        result = self.serial.publish(self.root, 1, sha, past, "仅测试回执", self.a)
        again = self.serial.publish(self.root, 1, sha, past, "仅测试回执", self.a)
        self.assertEqual(result["revision"], again["revision"])
        self.assertEqual(result["published_chapters"], 1)

    def test_snapshot_restores_serial_module_and_freeze_boundary(self):
        self.release(1); archive = Path(self.temp.name) / "snapshot.zip"
        ns.snapshot(self.root, archive)
        restored = Path(self.temp.name) / "restored"
        with zipfile.ZipFile(archive) as z: z.extractall(restored)
        run = subprocess.run([sys.executable, str(restored / "SYSTEM/tools/novel_state.py"), "--root", str(restored), "status"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["frozen_chapters"], 1)

    def test_plan_cannot_reset_existing_deadline(self):
        rec = self.record(1); rec['thread_events'] = [{'id': 'TEST', 'action': 'plant', 'description': '测试承诺', 'quote': '甲把钥匙交给乙。'}]
        self.release(1, record=rec)
        state = ns.load_state(self.root)
        plan = {'base_revision': state['revision'], 'roadmap_sha256': state['inputs']['roadmap']['sha256'],
                'plans': [{'id': 'TEST', 'deadline_chapter': 99, 'reason': '不允许覆盖'}]}
        path = Path(self.temp.name) / 'plan.json'; ns.atomic_json(path, plan)
        with self.assertRaises(ns.WorkspaceError): self.serial.plan(self.root, path, self.a)
        self.assertEqual(ns.load_state(self.root)['serialization']['promises']['TEST']['deadline_chapter'], 10)

    def test_context_projection_does_not_expand_with_closed_history(self):
        self.release(1); state = ns.load_state(self.root)
        state['serialization']['promises']['TEST'] = {'status': 'closed', 'deadline_chapter': 1, 'history': ['旧归档' * 20000]}
        projection = self.serial.continuation_state(self.root, state, self.a)
        self.assertNotIn('TEST', projection['promises'])
        self.assertLess(len(json.dumps(projection)), 3000)

    def test_100_chapter_900k_serial_release_finish_and_export(self):
        # 重复文本只压测100章流程/版本/冻结，绝不是小说质量验证。
        text = '甲把钥匙交给乙。' + '测' * 8993
        self.assertEqual(ns.han_count(text), 9000)
        for n in range(1, 101): self.release(n, text)
        state = ns.load_state(self.root); status = ns.status(self.root)
        self.assertEqual(status['actual_han'], 900000)
        self.assertEqual(status['frozen_chapters'], 100)
        self.assertEqual(status['published_chapters'], 0)
        self.assertEqual(status['reviewed_until'], 100)
        end = {'base_revision': state['revision'], 'decision': 'complete', 'blocking_issues': [],
               **{k: '合成收束流程测试' for k in ('main_conflict','character_choice','promise_payoffs','remaining_open_questions')},
               'evidence': [{'chapter': 100, 'sha256': state['chapters']['100']['text_sha256'], 'quote': '甲把钥匙交给乙。'}]}
        path = Path(self.temp.name) / 'finish.json'; ns.atomic_json(path, end)
        self.assertTrue(ns.finish(self.root, path)['completed'])
        exported = Path(self.temp.name) / 'export.txt'; ns.export_text(self.root, exported)
        self.assertEqual(exported.read_text().count('甲把钥匙交给乙。'), 100)
