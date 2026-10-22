import copy
import json
from pathlib import Path
from unittest.mock import patch
import zipfile

from test_novel_state import WorkspaceFixture, ns


class ReliabilityTests(WorkspaceFixture):
    # 针对V4的状态和故障场景，与原回归场景分开计数。
    def change_input(self, kind, text, base=None):
        path = Path(self.temp.name) / "new-input.md"
        path.write_text(text, encoding="utf-8")
        return ns.set_input(self.root, kind, path, "明确的测试变更", ns.load_state(self.root)["revision"] if base is None else base)

    def temporal(self, chapter, value, start, end=None, kind="state", holder=""):
        text = f"角色记录：{value}。"
        record = self.record(chapter, text)
        record["story_time_end"] = max(chapter, start)
        record["facts"] = [{"kind": kind, "key": "钥匙", "value": value, "holder": holder,
                            "valid_from": start, "valid_until": end, "quote": text}]
        return self.adopt(chapter, text, record=record)

    def test_time_query_uses_story_time_not_publication_order(self):
        self.temporal(1, "乙持有", 20)
        self.temporal(2, "甲持有", 10)
        self.assertEqual(ns.memory(self.root, 15)["facts"][0]["value"], "甲持有")
        self.assertEqual(ns.memory(self.root, 25)["facts"][0]["value"], "乙持有")

    def test_new_state_expiration_does_not_resurrect_old_state(self):
        self.temporal(1, "甲持有", 1)
        self.temporal(2, "乙持有", 2, 3)
        self.assertEqual(ns.memory(self.root, 4)["facts"], [])

    def test_character_knowledge_is_separate_from_belief_and_other_people(self):
        self.temporal(1, "知道钥匙位置", 1, kind="knowledge", holder="乙")
        self.temporal(2, "以为钥匙丢失", 1, kind="belief", holder="甲")
        facts = ns.memory(self.root, 2)["facts"]
        self.assertEqual({(f["kind"], f["holder"]) for f in facts}, {("knowledge", "乙"), ("belief", "甲")})

    def test_future_published_revelation_is_not_visible_when_querying_earlier_prefix(self):
        self.temporal(1, "甲持有", 1)
        self.temporal(2, "乙持有", 2)
        self.assertEqual(ns.memory(self.root, 2, through=1)["facts"][0]["value"], "甲持有")

    def test_conflicting_values_are_reported_not_silently_selected(self):
        self.temporal(1, "甲持有", 1)
        self.temporal(2, "乙持有", 1)
        self.assertEqual(len(ns.memory(self.root, 2)["conflicts"]), 1)
        self.assertEqual(ns.memory(self.root, 2)["facts"], [])

    def test_missing_holder_is_rejected(self):
        record = self.record(1); record["facts"][0]["kind"] = "knowledge"
        with self.assertRaises(ns.WorkspaceError): self.adopt(1, record=record)

    def test_invalid_time_interval_is_rejected(self):
        record = self.record(1); record["facts"][0]["valid_until"] = 1
        with self.assertRaises(ns.WorkspaceError): self.adopt(1, record=record)

    def test_future_plan_cannot_become_current_fact(self):
        record = self.record(1); record["facts"][0]["valid_from"] = 99
        with self.assertRaises(ns.WorkspaceError): self.adopt(1, record=record)

    def test_partial_payoff_stays_open(self):
        record = self.record(1)
        record["thread_events"] = [{"id": "T1", "action": "partial", "description": "只解决一部分", "quote": "甲把钥匙交给乙。"}]
        self.adopt(1, record=record)
        self.assertEqual(ns.memory(self.root)["open_threads"][0]["id"], "T1")

    def test_canon_change_invalidates_existing_chapters_and_checkpoint(self):
        self.adopt(1); self.adopt(2); self.checkpoint(1, 2)
        self.change_input("canon", "作者明确修改了能力边界。")
        self.assertEqual(ns.status(self.root)["needs_revalidation"], [1, 2])
        self.assertEqual(ns.status(self.root)["reviewed_until"], 0)
        self.assertEqual(ns.memory(self.root)["through_chapter"], 0)

    def test_future_roadmap_update_keeps_adopted_facts(self):
        self.adopt(1)
        self.change_input("roadmap", "只调整未写路线。")
        self.assertEqual(ns.status(self.root)["needs_revalidation"], [])
        self.assertEqual(len(ns.memory(self.root)["facts"]), 1)

    def test_empty_input_does_not_overwrite_valid_input(self):
        old = copy.deepcopy(ns.load_state(self.root)["inputs"])
        with self.assertRaises(ns.WorkspaceError): self.change_input("canon", " ")
        self.assertEqual(ns.load_state(self.root)["inputs"], old)

    def test_stale_input_update_is_rejected(self):
        old = ns.load_state(self.root)["revision"]
        self.adopt(1)
        with self.assertRaises(ns.WorkspaceError): self.change_input("roadmap", "旧任务", base=old)

    def test_changes_invalidate_beyond_thirty_chapters(self):
        state = ns.load_state(self.root); state["config"]["review_interval"] = 999
        ns.atomic_json(self.root / "state.json", state)
        for n in range(1, 36): self.adopt(n)
        self.adopt(1, "甲把钥匙留在门外。")
        self.assertEqual(ns.status(self.root)["needs_revalidation"], list(range(2, 36)))
        with self.assertRaises(ns.WorkspaceError): ns.memory(self.root, through=35)

    def test_repair_count_persists_and_fourth_repair_is_rejected(self):
        path = Path(self.temp.name) / "candidate.txt"
        path.write_text("初稿", encoding="utf-8"); ns.candidate(self.root, 1, path)
        for n in range(1, 4):
            path.write_text(f"修改稿{n}", encoding="utf-8")
            ns.candidate(self.root, 1, path, request=f"修复问题{n}", repairing=True)
        self.assertEqual(ns.load_state(self.root)["tasks"]["1"]["repairs"], 3)
        path.write_text("第四次修改", encoding="utf-8")
        with self.assertRaises(ns.WorkspaceError): ns.candidate(self.root, 1, path, request="第四个问题", repairing=True)
        self.assertEqual(ns.load_state(self.root)["tasks"]["1"]["repairs"], 3)

    def test_candidate_cannot_reset_open_task_counter(self):
        path = Path(self.temp.name) / "candidate.txt"
        path.write_text("初稿", encoding="utf-8"); ns.candidate(self.root, 1, path)
        path.write_text("新建任务绕过次数", encoding="utf-8")
        with self.assertRaises(ns.WorkspaceError): ns.candidate(self.root, 1, path, "edit", "另起名字")

    def test_repeated_candidate_is_idempotent(self):
        path = Path(self.temp.name) / "candidate.txt"; path.write_text("候选稿", encoding="utf-8")
        first = ns.candidate(self.root, 1, path)
        second = ns.candidate(self.root, 1, path)
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(second["history"]), 1)

    def test_repeated_adoption_does_not_duplicate_chapter_or_revision(self):
        record = self.record(1)
        self.adopt(1, record=record)
        before = ns.load_state(self.root)["revision"]
        path = Path(self.temp.name) / "draft.txt"; report = Path(self.temp.name) / "record.json"
        result = ns.adopt(self.root, path, report)
        self.assertTrue(result["already_adopted"])
        self.assertEqual(ns.load_state(self.root)["revision"], before)

    def test_failure_before_manifest_commit_leaves_old_adoption_valid(self):
        self.adopt(1)
        old = copy.deepcopy(ns.load_state(self.root))
        path = Path(self.temp.name) / "replacement.txt"; text = "甲把钥匙留在门外。"; path.write_text(text, encoding="utf-8")
        ns.candidate(self.root, 1, path, "edit", "明确修改请求")
        record = self.record(1, text); report = Path(self.temp.name) / "replacement.json"; report.write_text(json.dumps(record), encoding="utf-8")
        with patch.object(ns, "atomic_json", side_effect=OSError("模拟提交前失败")):
            with self.assertRaises(OSError): ns.adopt(self.root, path, report)
        self.assertEqual(ns.load_state(self.root)["chapters"], old["chapters"])
        ns.verify_files(self.root, ns.load_state(self.root))

    def test_writer_lock_rejects_concurrent_mutation(self):
        with ns.writer_lock(self.root):
            with self.assertRaises(ns.WorkspaceError): self.change_input("roadmap", "第二个写入者")

    def test_snapshot_preserves_pending_work_and_can_be_verified(self):
        self.adopt(1)
        path = Path(self.temp.name) / "candidate.txt"; path.write_text("第二章未完成候选", encoding="utf-8")
        ns.candidate(self.root, 2, path)
        output = Path(self.temp.name) / "snapshot.zip"
        result = ns.snapshot(self.root, output)
        self.assertTrue(result["valid"])
        with zipfile.ZipFile(output) as archive:
            state = json.loads(archive.read("state.json"))
            self.assertEqual(state["tasks"]["2"]["phase"], "candidate")
            self.assertEqual(len(state["chapters"]), 1)

    def test_snapshot_corruption_is_detected(self):
        self.adopt(1)
        path = Path(self.temp.name) / "snapshot.zip"; ns.snapshot(self.root, path)
        with zipfile.ZipFile(path) as archive: files = {n: archive.read(n) for n in archive.namelist()}
        files["state.json"] = b'{}'
        with zipfile.ZipFile(path, "w") as archive:
            for n, data in files.items(): archive.writestr(n, data)
        with self.assertRaises(ns.WorkspaceError): ns.verify_snapshot(path)

    def test_snapshot_rejects_output_inside_book(self):
        with self.assertRaises(ns.WorkspaceError): ns.snapshot(self.root, self.root / "snapshot.zip")

    def test_context_budget_is_respected(self):
        self.adopt(1)
        packet = ns.context(self.root, budget=5000)
        self.assertLessEqual(len(packet), 5000)

    def test_export_uses_adopted_versions_and_excludes_candidate(self):
        self.adopt(1)
        path = Path(self.temp.name) / "candidate.txt"; path.write_text("废稿不应导出", encoding="utf-8")
        ns.candidate(self.root, 2, path)
        output = Path(self.temp.name) / "story.txt"; ns.export_text(self.root, output)
        self.assertIn("甲把钥匙交给乙。", output.read_text(encoding="utf-8"))
        self.assertNotIn("废稿", output.read_text(encoding="utf-8"))

    def test_completion_rejects_insufficient_chapters(self):
        self.adopt(1)
        path = Path(self.temp.name) / "finish.json"; path.write_text(json.dumps({"base_revision": ns.load_state(self.root)["revision"]}), encoding="utf-8")
        with self.assertRaises(ns.WorkspaceError): ns.finish(self.root, path)

    def test_immutable_source_replacement_is_detected(self):
        state = ns.load_state(self.root)
        path = self.root / state["inputs"]["canon"]["path"]
        path.write_text("被外部直接覆盖", encoding="utf-8")
        with self.assertRaises(ns.WorkspaceError): ns.status(self.root)
