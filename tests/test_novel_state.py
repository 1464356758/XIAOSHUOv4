import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("novel_state", Path(__file__).parents[1] / "tools" / "novel_state.py")
ns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ns)


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "book"
        ns.init(self.root, "测试书", {"strict_length": False, "review_interval": 5})
        source = Path(self.temp.name) / "input.md"
        for kind in ("canon", "roadmap"):
            source.write_text("测试资料：仅用于机制验证，不是小说。", encoding="utf-8")
            ns.set_input(self.root, kind, source, "测试初始化", ns.load_state(self.root)["revision"])

    def tearDown(self):
        self.temp.cleanup()

    def record(self, chapter, text="甲把钥匙交给乙。", stage="S01"):
        state = ns.load_state(self.root)
        return {"chapter": chapter, "title": "测试章", "stage": stage,
                "base_revision": state["revision"], "text_sha256": ns.digest(text), "complete": True,
                "canon_sha256": state["inputs"]["canon"]["sha256"], "story_time_end": chapter,
                "summary": "钥匙转交，原持有人失去直接开门的条件。",
                "review": {"read_full_text": True, "decision": "adopt", "blocking_issues": [],
                           "sources_checked": [], "conclusion": "本章引文与记录对应。"},
                "facts": [{"kind": "state", "key": "钥匙归属", "value": "乙", "quote": text, "valid_from": chapter, "valid_until": None, "holder": ""}],
                "progression": {key: "测试记录" for key in ("goal", "obstacle", "method", "choice", "result", "consequence", "next_options", "scene_function")},
                "thread_events": []}

    def adopt(self, chapter, text="甲把钥匙交给乙。", stage="S01", record=None, revalidate=False):
        record = record or self.record(chapter, text, stage)
        path = Path(self.temp.name) / "draft.txt"
        report = Path(self.temp.name) / "record.json"
        path.write_text(text, encoding="utf-8")
        if not revalidate:
            mode = "edit" if str(chapter) in ns.load_state(self.root)["chapters"] else "write"
            ns.candidate(self.root, chapter, path, mode, "测试明确改稿请求" if mode == "edit" else "")
        report.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return ns.adopt(self.root, path, report, revalidate)

    def checkpoint(self, start, end, kind="window", decision="continue", override=None):
        state = ns.load_state(self.root)
        report = {"base_revision": state["revision"], "from": start, "to": end, "kind": kind,
                  "canon_sha256": state["inputs"]["canon"]["sha256"],
                  "read_mode": "full_text_batches", "decision": decision,
                  "coverage": {str(n): state["chapters"][str(n)]["text_sha256"] for n in range(start, end + 1)},
                  "record_coverage": {str(n): state["chapters"][str(n)]["record_sha256"] for n in range(start, end + 1)},
                  "findings": [] if decision == "continue" else ["测试问题"],
                  **{key: "具体检查结论" for key in ("conclusion", "causal_progress", "repetition_comparison", "character_changes", "promise_payoffs", "ending_runway", "next_action")}}
        report.update(override or {})
        path = Path(self.temp.name) / "checkpoint.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return ns.checkpoint(self.root, path)


class WorkspaceTests(WorkspaceFixture):
    def test_han_count_excludes_latin_and_punctuation(self):
        self.assertEqual(ns.han_count("甲A1，乙。𠀀"), 3)

    def test_complete_adoption_and_source_packet(self):
        self.adopt(1)
        self.assertEqual(ns.status(self.root)["adopted_chapters"], 1)
        packet = ns.context(self.root, "钥匙")
        self.assertIn("甲把钥匙交给乙。", packet)
        self.assertIn("state", packet)

    def test_init_never_overwrites_progress(self):
        self.adopt(1)
        with self.assertRaises(ns.WorkspaceError):
            ns.init(self.root, "另一本书")
        self.assertEqual(ns.status(self.root)["adopted_chapters"], 1)

    def test_stale_record_cannot_be_adopted(self):
        record = self.record(2)
        self.adopt(1)
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(2, record=record)

    def test_body_hash_mismatch_is_rejected(self):
        record = self.record(1)
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(1, "乙没有拿到钥匙。", record=record)

    def test_unfinished_prose_is_not_adopted(self):
        record = self.record(1); record["complete"] = False
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(1, record=record)

    def test_fabricated_quote_is_rejected(self):
        record = self.record(1); record["facts"][0]["quote"] = "钥匙留在甲手里。"
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(1, record=record)

    def test_plan_is_not_allowed_as_fact_kind(self):
        record = self.record(1); record["facts"][0]["kind"] = "plan"
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(1, record=record)

    def test_empty_summary_is_rejected_without_destroying_old_record(self):
        self.adopt(1)
        record = self.record(1); record["summary"] = ""
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(1, record=record)
        self.assertEqual(ns.status(self.root)["adopted_chapters"], 1)

    def test_skipping_a_chapter_is_rejected(self):
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(2)

    def test_fifth_chapter_requires_review_before_sixth(self):
        for n in range(1, 6):
            self.adopt(n)
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(6)
        self.checkpoint(1, 5)
        self.adopt(6)
        self.assertEqual(ns.status(self.root)["adopted_chapters"], 6)

    def test_partial_review_cannot_clear_due_review(self):
        for n in range(1, 6):
            self.adopt(n)
        self.checkpoint(5, 5)
        self.assertTrue(ns.status(self.root)["window_review_due"])

    def test_summary_only_cannot_count_as_full_review(self):
        self.adopt(1)
        with self.assertRaises(ns.WorkspaceError):
            self.checkpoint(1, 1, override={"read_mode": "summaries_only"})

    def test_review_coverage_is_exact(self):
        self.adopt(1); self.adopt(2)
        with self.assertRaises(ns.WorkspaceError):
            self.checkpoint(1, 2, override={"coverage": {"1": ns.load_state(self.root)["chapters"]["1"]["text_sha256"]}})

    def test_stage_transition_needs_stage_review(self):
        self.adopt(1)
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(2, stage="S02")
        self.checkpoint(1, 1, kind="stage")
        self.adopt(2, stage="S02")

    def test_prior_change_marks_later_chapters_and_excludes_them_from_continuation(self):
        self.adopt(1); self.adopt(2)
        self.adopt(1, "甲把钥匙放在桌上。")
        self.assertEqual(ns.status(self.root)["needs_revalidation"], [2])
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(3)
        with self.assertRaises(ns.WorkspaceError):
            ns.context(self.root)
        self.adopt(2, revalidate=True)
        self.adopt(3)

    def test_changed_record_invalidates_old_review(self):
        self.adopt(1)
        self.checkpoint(1, 1)
        record = self.record(1); record["summary"] = "修正后的章节摘要。"
        self.adopt(1, record=record)
        self.assertEqual(ns.status(self.root)["reviewed_until"], 0)

    def test_blocking_stage_review_stops_new_prose(self):
        self.adopt(1)
        self.checkpoint(1, 1, decision="repair_required")
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(2)
        self.checkpoint(1, 1)
        self.adopt(2)

    def test_tampered_adopted_file_is_detected(self):
        self.adopt(1)
        path = self.root / ns.load_state(self.root)["chapters"]["1"]["text_path"]
        path.write_text("外部修改", encoding="utf-8")
        with self.assertRaises(ns.WorkspaceError):
            ns.status(self.root)

    def test_strict_length_is_measured(self):
        state = ns.load_state(self.root); state["config"]["strict_length"] = True
        ns.atomic_json(self.root / "state.json", state)
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(1)

    def test_budget_overflow_is_explicit(self):
        self.adopt(1)
        with self.assertRaises(ns.WorkspaceError):
            ns.context(self.root, budget=10)

    def test_checked_source_requires_current_text_and_real_quote(self):
        self.adopt(1)
        record = self.record(2)
        record["review"]["sources_checked"] = [{"chapter": 1, "sha256": ns.digest("甲把钥匙交给乙。"), "quote": "从未出现的句子"}]
        with self.assertRaises(ns.WorkspaceError):
            self.adopt(2, record=record)


if __name__ == "__main__":
    unittest.main()
