#!/usr/bin/env python3
"""100×9000汉字合成数据验证；不调用模型、不验证文学质量。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import zipfile

spec = importlib.util.spec_from_file_location("novel_state_v4", Path(__file__).with_name("novel_state.py"))
ns = importlib.util.module_from_spec(spec); spec.loader.exec_module(ns)


def run():
    with tempfile.TemporaryDirectory(prefix="novel-v4-900k-") as directory:
        base = Path(directory); root = base / "book"
        ns.init(root, "合成数据机制测试")
        source = base / "input.md"; source.write_text("合成数据，不是小说。统一时间刻度为测试序号。", encoding="utf-8")
        for kind in ("canon", "roadmap"):
            ns.set_input(root, kind, source, "机制测试", ns.load_state(root)["revision"])
        body = "测试" * 4500
        draft = base / "draft.txt"; draft.write_text(body, encoding="utf-8")
        record_path = base / "record.json"; report_path = base / "review.json"

        def review(start, end, kind):
            state = ns.load_state(root)
            report = {"base_revision": state["revision"], "canon_sha256": state["inputs"]["canon"]["sha256"],
                      "from": start, "to": end, "kind": kind, "read_mode": "full_text_batches", "decision": "continue",
                      "coverage": {str(n): state["chapters"][str(n)]["text_sha256"] for n in range(start, end + 1)},
                      "record_coverage": {str(n): state["chapters"][str(n)]["record_sha256"] for n in range(start, end + 1)},
                      "findings": [], **{k: "人工构造机制输入，不是实际语义审查" for k in ("conclusion", "causal_progress", "repetition_comparison", "character_changes", "promise_payoffs", "ending_runway", "next_action")}}
            report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            ns.checkpoint(root, report_path)

        for chapter in range(1, 101):
            state = ns.load_state(root)
            ns.candidate(root, chapter, draft)
            record = {"chapter": chapter, "title": "合成数据", "stage": f"S{(chapter-1)//10+1:02d}",
                      "base_revision": state["revision"], "canon_sha256": state["inputs"]["canon"]["sha256"],
                      "text_sha256": ns.digest(body), "complete": True, "story_time_end": chapter,
                      "summary": "合成测试数据，不是故事摘要。",
                      "review": {"read_full_text": True, "decision": "adopt", "blocking_issues": [], "sources_checked": [], "conclusion": "构造字段，不是GPT实际阅读"},
                      "facts": [{"kind": "state", "key": "test:state", "value": str(chapter), "holder": "", "valid_from": chapter, "valid_until": None, "quote": "测试"}],
                      "progression": {k: "机制测试" for k in ("goal", "obstacle", "method", "choice", "result", "consequence", "next_options", "scene_function")}, "thread_events": []}
            record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            ns.adopt(root, draft, record_path)
            if chapter % 5 == 0: review(chapter-4, chapter, "window")
            if chapter % 10 == 0: review(chapter-9, chapter, "stage")

        state = ns.load_state(root)
        ending = {"base_revision": state["revision"], "decision": "complete", "blocking_issues": [],
                  **{k: "构造结论，仅用于机制测试" for k in ("main_conflict", "character_choice", "promise_payoffs", "remaining_open_questions")},
                  "evidence": [{"chapter": 100, "sha256": state["chapters"]["100"]["text_sha256"], "quote": "测试"}]}
        report_path.write_text(json.dumps(ending, ensure_ascii=False), encoding="utf-8")
        completed = ns.finish(root, report_path)
        exported = ns.export_text(root, base / "story.txt")
        packet = ns.context(root, "test", budget=40000)
        saved = ns.snapshot(root, base / "snapshot.zip")
        restored = base / "restored"
        with zipfile.ZipFile(base / "snapshot.zip") as archive: archive.extractall(restored)
        restored_spec = importlib.util.spec_from_file_location("restored_novel", restored / "SYSTEM/tools/novel_state.py")
        restored_tool = importlib.util.module_from_spec(restored_spec); restored_spec.loader.exec_module(restored_tool)
        status = restored_tool.status(restored)
        assert status["actual_han"] == 900000 and status["adopted_chapters"] == 100 and status["completed"]
        assert saved["valid"] and completed["completed"] and exported["chapters"] == 100 and len(packet) <= 40000
        return {"synthetic_only": True, "chapters": 100, "han_per_chapter": 9000, "actual_han": status["actual_han"],
                "window_reviews": 20, "stage_reviews": 10, "completion_gate": "passed", "export": "passed",
                "snapshot_integrity": "passed", "restore_with_bundled_tool": "passed", "context_characters": len(packet),
                "model_calls": 0, "literary_quality_evaluated": False, "note": "阅读声明和报告均为人工构造；不是90万字小说质量验证。"}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
