#!/usr/bin/env python3
"""GPT 小说工作区的确定性文件助手；不调用模型、不判定文学质量。

正文、引用和采用记录保存在工作区。GPT 负责阅读、写作和语义判断，
本工具负责字数、版本绑定、引用存在性、检查覆盖和恢复边界。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
import zipfile
from types import SimpleNamespace


class WorkspaceError(ValueError):
    pass


_SERIAL_MODULE = None


def serial_module():
    global _SERIAL_MODULE
    if _SERIAL_MODULE is None:
        spec = importlib.util.spec_from_file_location("novel_serial_release", Path(__file__).with_name("serial_release.py"))
        _SERIAL_MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SERIAL_MODULE)
    return _SERIAL_MODULE


def serial_api():
    names = ("require", "digest", "han_count", "read_json", "atomic_json", "immutable_text", "inside",
             "load_state", "verify_files", "writer_lock", "validate_record", "records", "quote_exists", "review_status")
    return SimpleNamespace(**{name: globals()[name] for name in names})


def require(condition, message):
    if not condition:
        raise WorkspaceError(message)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def han_count(text):
    ranges = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
              (0x20000, 0x2EBEF), (0x30000, 0x323AF))
    return sum(any(lo <= ord(c) <= hi for lo, hi in ranges) for c in text)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def immutable_text(path, text):
    path = Path(path)
    if path.exists():
        require(path.read_text(encoding="utf-8") == text, "不可变产物发生冲突。")
    else:
        atomic_text(path, text)


@contextlib.contextmanager
def writer_lock(root):
    """单写入进程；崩溃残留锁需核对无运行进程后手动移除。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".writer-lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise WorkspaceError("已有写入锁；先核对正在运行的任务，不重复提交。") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    require(path.is_relative_to(root), "引用不能离开当前工作区。")
    return path


def load_state(root):
    require(not (Path(root) / "private_storage.json").exists(),
            "本书已启用私密稿；先用tools/private_book.py status查看进度，再用私密钥匙unlock到新工作目录。公开state.json是迁入前基线，不能据此重写。")
    path = Path(root) / "state.json"
    require(path.exists(), "本书尚未初始化；先执行 init。")
    state = read_json(path)
    require(state.get("schema_version") == 2, "不支持的工作区格式；旧版请按迁移说明导入副本。")
    return state


def verify_files(root, state):
    for item in state["inputs"].values():
        path = inside(root, item["path"])
        require(path.is_file() and digest(path.read_text(encoding="utf-8")) == item["sha256"], "规划或作者设定与登记版本不符。")
    for key, chapter in state["chapters"].items():
        for field, sha_field in (("text_path", "text_sha256"), ("record_path", "record_sha256")):
            path = inside(root, chapter[field])
            require(path.is_file(), f"第 {key} 章缺少文件：{chapter[field]}")
            require(digest(path.read_text(encoding="utf-8")) == chapter[sha_field],
                    f"第 {key} 章文件与采用版本不符：{chapter[field]}")
    if state.get("serialization", {}).get("enabled"):
        serial_module().verify_frozen(state, serial_api())


def init(root, title, overrides=None):
    require(bool(title.strip()), "书名或临时项目名不能为空。")
    with writer_lock(root):
        require(not (Path(root) / "state.json").exists(), "已有本书进度，禁止重新初始化覆盖。")
        config = {"target_chapters": 100, "target_han": 900000,
                  "chapter_han_min": 8000, "chapter_han_max": 10000,
                  "book_han_min": 880000, "book_han_max": 920000,
                  "strict_length": True, "review_interval": 5, "max_local_repairs": 3,
                  "serial_mode": False, "serial_stage_size": 10, "serial_lookahead": 3,
                  "opening_hook_max_han": 800, "opening_core_max_han": 3000,
                  "opening_impact_max_han": 3000}
        config.update(overrides or {})
        require(config["review_interval"] >= 1, "回看间隔必须为正数。")
        require(0 <= config["chapter_han_min"] <= config["chapter_han_max"], "章长区间无效。")
        require(isinstance(config["target_chapters"], int) and not isinstance(config["target_chapters"], bool) and config["target_chapters"] > 0, "目标章数必须为正整数。")
        require(isinstance(config["max_local_repairs"], int) and 0 <= config["max_local_repairs"] <= 3, "自动局部修订上限为0—3。")
        require(0 <= config["book_han_min"] <= config["book_han_max"], "全书字数范围无效。")
        state = {"schema_version": 2, "book_id": str(uuid.uuid4()), "title": title,
                 "revision": 0, "config": config, "chapters": {},
                 "checkpoints": [], "needs_revalidation": [], "inputs": {},
                 "input_history": [], "tasks": {}, "completion": None}
        if config["serial_mode"]:
            state["serialization"] = serial_module().new_state(config, serial_api())
        atomic_json(Path(root) / "state.json", state)
        return state


def records(root, state):
    return [(int(n), read_json(inside(root, item["record_path"])))
            for n, item in sorted(state["chapters"].items(), key=lambda x: int(x[0]))]


def valid_checkpoint(state, checkpoint):
    return (checkpoint.get("canon_sha256") == state["inputs"].get("canon", {}).get("sha256")
            and all(state["chapters"].get(str(n), {}).get("text_sha256") == sha
                for n, sha in checkpoint["coverage"].items())
            and all(state["chapters"].get(str(n), {}).get("record_sha256") == sha
                    for n, sha in checkpoint["record_coverage"].items()))


def review_status(state):
    last = len(state["chapters"])
    valid = [c for c in state["checkpoints"] if valid_checkpoint(state, c)]
    reviewed_until = 0
    for checkpoint in sorted(valid, key=lambda c: (c["from"], c["to"])):
        if checkpoint["from"] <= reviewed_until + 1:
            reviewed_until = max(reviewed_until, checkpoint["to"])
    # 最新检查覆盖了同一段问题后，旧检查的未解决标记才被替代。
    unresolved = []
    for idx, checkpoint in enumerate(valid):
        if checkpoint["decision"] not in ("repair_required", "author_decision"):
            continue
        resolved = any(new["decision"] in ("continue", "adjust_future")
                       and new["from"] <= checkpoint["from"] and new["to"] >= checkpoint["to"]
                       for new in valid[idx + 1:])
        if not resolved:
            unresolved.append({"from": checkpoint["from"], "to": checkpoint["to"],
                               "decision": checkpoint["decision"], "conclusion": checkpoint["conclusion"]})
    return {"reviewed_until": reviewed_until,
            "window_review_due": last - reviewed_until >= state["config"]["review_interval"],
            "unresolved_reviews": unresolved}


def status(root):
    state = load_state(root)
    verify_files(root, state)
    return {"book_id": state["book_id"], "title": state["title"], "revision": state["revision"],
            "adopted_chapters": len(state["chapters"]),
            "actual_han": sum(c["han_count"] for c in state["chapters"].values()),
            "open_tasks": {n: t for n, t in state["tasks"].items() if t["phase"] == "candidate"},
            "completed": state["completion"] is not None,
            "inputs": state["inputs"],
            "needs_revalidation": state["needs_revalidation"], **review_status(state),
            "literary_quality": "程序未评判；必须由GPT与作者依据正文判断",
            **(serial_module().summary(state) if state.get("serialization", {}).get("enabled") else {})}


def quote_exists(text, quote):
    return isinstance(quote, str) and bool(quote.strip()) and quote in text


def set_input(root, kind, path, reason, base_revision):
    require(kind in ("canon", "roadmap") and bool(reason.strip()), "资料类别或变更原因无效。")
    content = Path(path).read_text(encoding="utf-8")
    require(bool(content.strip()), "空资料不能覆盖有效设定。")
    with writer_lock(root):
        state = load_state(root); verify_files(root, state)
        require(base_revision == state["revision"], "资料修改基于旧进度。")
        require(state["completion"] is None, "完成后先reopen再修改资料。")
        sha = digest(content)
        old = state["inputs"].get(kind)
        if old and old["sha256"] == sha:
            return {"revision": state["revision"], "unchanged": True}
        require(not (kind == "canon" and state.get("serialization", {}).get("frozen")),
                "连载定稿已冻结，不能改canon并重写既往。保留已发生事实，调整未来roadmap；确有旧错用incident登记前向处理。")
        relative = f"inputs/{kind}_{sha}.md"
        target = inside(root, relative); target.parent.mkdir(parents=True, exist_ok=True)
        immutable_text(target, content)
        item = {"kind": kind, "path": relative, "sha256": sha, "reason": reason}
        state["inputs"][kind] = item
        state["input_history"].append(item)
        if old and kind == "canon":
            state["needs_revalidation"] = list(range(1, len(state["chapters"]) + 1))
        state["revision"] += 1
        atomic_json(Path(root) / "state.json", state)
        return {"revision": state["revision"], "sha256": sha, "needs_revalidation": state["needs_revalidation"]}


def candidate(root, chapter, text_path, mode="write", request="", repairing=False):
    text = Path(text_path).read_text(encoding="utf-8")
    require(isinstance(chapter, int) and not isinstance(chapter, bool) and chapter >= 1 and bool(text.strip()), "章节或候选正文无效。")
    with writer_lock(root):
        state = load_state(root); verify_files(root, state)
        require(state["completion"] is None, "完成后先reopen再改稿。")
        if state.get("serialization", {}).get("enabled"):
            serial_module().guard_chapter(state, chapter, serial_api())
        previous = state["chapters"].get(str(chapter))
        stage = previous["stage"] if previous else state["chapters"].get(str(len(state["chapters"])), {}).get("stage", "S01")
        ensure_next_allowed(state, chapter, stage)
        current = state["tasks"].get(str(chapter))
        sha = digest(text)
        if current and current["phase"] == "candidate":
            if current["candidate_sha256"] == sha:
                return dict(current, unchanged=True)
            require(repairing, "当前已有候选；修改正文应使用repair，不能重开任务清零次数。")
            require(bool(request.strip()), "修订需要具体问题与修改原因。")
            require(current["repairs"] < state["config"]["max_local_repairs"], "已到局部修订上限；保留当前稿并先处理结构或作者决定。")
            current["repairs"] += 1
        else:
            require(not repairing, "先登记初稿，不能直接登记修订。")
            if previous:
                require(mode == "edit" and bool(request.strip()), "改动已采用章应明确edit及作者请求，不能当新章重写。")
            else:
                require(mode == "write", "新章只能使用write。")
            current = {"task_id": str(uuid.uuid4()), "chapter": chapter, "mode": mode,
                       "phase": "candidate", "repairs": 0, "history": []}
        relative = f"candidates/CH{chapter:03d}_{sha}.txt"
        target = inside(root, relative); target.parent.mkdir(parents=True, exist_ok=True)
        immutable_text(target, text)
        current["candidate_sha256"] = sha
        current["candidate_path"] = relative
        current["history"].append({"sha256": sha, "reason": request or "初稿", "repairs": current["repairs"]})
        state["tasks"][str(chapter)] = current
        # revision追踪采用正文、资料及审查的版本；候选次数独立持久保存，不改变正式进度。
        atomic_json(Path(root) / "state.json", state)
        return current


def memory(root, story_time=None, through=None):
    state = load_state(root); verify_files(root, state)
    frontier = min(state["needs_revalidation"], default=len(state["chapters"]) + 1) - 1
    through = frontier if through is None else through
    require(isinstance(through, int) and 0 <= through <= frontier, "不能把待重核章节当有效记忆。")
    items = [(n, r) for n, r in records(root, state) if n <= through]
    if story_time is None:
        story_time = items[-1][1]["story_time_end"] if items else 0
    groups, threads = {}, {}
    for n, record in items:
        sha = state["chapters"][str(n)]["text_sha256"]
        for fact in record["facts"]:
            group = (fact["kind"], fact.get("holder", ""), fact["key"])
            groups.setdefault(group, []).append(dict(fact, known_at_chapter=n, source_sha256=sha))
        for event in record["thread_events"]:
            threads[event["id"]] = dict(event, source_chapter=n, source_sha256=sha)
    active, conflicts = [], []
    for group, events in groups.items():
        eligible = [f for f in events if f["valid_from"] <= story_time]
        if not eligible: continue
        last_start = max(f["valid_from"] for f in eligible)
        # 后来的状态起点截断旧状态；最新状态过期时，不把旧持有者复活。
        latest = [f for f in eligible if f["valid_from"] == last_start]
        latest = [f for f in latest if f["valid_until"] is None or story_time < f["valid_until"]]
        if len({f["value"] for f in latest}) > 1:
            conflicts.append({"key": list(group), "candidates": latest})
        else:
            active.extend(latest[-1:])
    return {"through_chapter": through, "story_time": story_time, "facts": active,
            "conflicts": conflicts, "open_threads": [t for t in threads.values() if t["action"] not in ("payoff", "close")],
            "note": "这是来源绑定的候选记忆；引文支持程度与人物知情边界仍需阅读判断。"}


def validate_record(root, state, text, record):
    require(set(state["inputs"]) == {"canon", "roadmap"}, "先登记作者设定canon与故事路线roadmap。")
    require(record.get("canon_sha256") == state["inputs"]["canon"]["sha256"], "记录所依据的作者设定已过期。")
    require(record.get("base_revision") == state["revision"], "写后记录基于旧进度；重新核对当前事实。")
    chapter = record.get("chapter")
    require(isinstance(chapter, int) and not isinstance(chapter, bool) and chapter >= 1, "章节序号无效。")
    require(record.get("text_sha256") == digest(text), "写后记录对应的不是当前正文。")
    require(record.get("complete") is True, "正文未完成，不能记为采用稿。")
    require(bool(record.get("title", "").strip()) and bool(record.get("stage", "").strip()), "缺少标题或阶段编号。")
    require(bool(record.get("summary", "").strip()), "章节摘要为空；保留旧记录，不能覆盖。")
    require(isinstance(record.get("story_time_end"), int) and not isinstance(record["story_time_end"], bool), "缺少统一故事时间轴上的整数位置。")
    if state.get("serialization", {}).get("enabled") and chapter == len(state["chapters"]) + 1 and chapter > 1:
        previous = read_json(inside(root, state["chapters"][str(chapter - 1)]["record_path"]))
        require(record["story_time_end"] >= previous["story_time_end"], "连载章末时间倒退；倒叙须保留当前章末位置，逐事实记录其真实生效时间。")
    review = record.get("review", {})
    require(review.get("read_full_text") is True, "尚未声明完整阅读本章。")
    require(review.get("decision") == "adopt" and review.get("blocking_issues") == [], "本章仍有必须处理的问题。")
    require(bool(review.get("conclusion", "").strip()), "缺少基于正文的检查结论。")
    require(isinstance(review.get("sources_checked"), list), "缺少已回查来源清单。")
    for source in review["sources_checked"]:
        item = state["chapters"].get(str(source.get("chapter")))
        require(item is not None, "回查来源不属于已采用章节。")
        require(source.get("sha256") == item["text_sha256"], "回查来源版本已经改变。")
        require(quote_exists(inside(root, item["text_path"]).read_text(encoding="utf-8"), source.get("quote")),
                "来源引文不在所引用的采用正文内。")
    require(isinstance(record.get("facts"), list), "事实候选必须为列表。")
    for fact in record["facts"]:
        require(fact.get("kind") in ("fact", "state", "knowledge", "belief"), "未来计划不能存成正文事实。")
        require(bool(fact.get("key", "").strip()) and bool(fact.get("value", "").strip()), "事实条目缺少键或内容。")
        require(quote_exists(text, fact.get("quote")), "事实候选的引文不在当前正文内。")
        start, end = fact.get("valid_from"), fact.get("valid_until")
        require(isinstance(start, int) and not isinstance(start, bool), "事实缺少故事时间valid_from。")
        require(end is None or (isinstance(end, int) and not isinstance(end, bool) and end > start), "事实有效区间应为左闭右开。")
        require(start <= record["story_time_end"], "未来生效的计划不能登记为本章已成立事实。")
        if fact["kind"] in ("knowledge", "belief"):
            require(bool(fact.get("holder", "").strip()), "知情或猜测必须注明具体人物。")
    progression = record.get("progression", {})
    for key in ("goal", "obstacle", "method", "choice", "result", "consequence", "next_options", "scene_function"):
        require(bool(progression.get(key, "").strip()), f"缺少推进记录：{key}。可以如实填写本章未改变。")
    require(isinstance(record.get("thread_events"), list), "伏笔事件必须为列表。")
    for event in record["thread_events"]:
        require(event.get("action") in ("plant", "develop", "partial", "payoff", "close"), "伏笔事件类型无效。")
        require(bool(event.get("id", "").strip()) and bool(event.get("description", "").strip()), "伏笔事件缺少编号或说明。")
        require(quote_exists(text, event.get("quote")), "伏笔事件的引文不在当前正文内。")
    count = han_count(text)
    config = state["config"]
    if config["strict_length"]:
        require(config["chapter_han_min"] <= count <= config["chapter_han_max"], f"实测汉字数 {count} 不在配置区间。")
    return count


def ensure_next_allowed(state, chapter, stage):
    last = len(state["chapters"])
    require(chapter <= last + 1, "不能跳过缺失章节。")
    require(chapter <= state["config"]["target_chapters"], "已经达到目标章数，先完成收束检查。")
    require(state["completion"] is None, "作品已完成；修改正文应先用reopen明确重开交付。")
    require(not any(n < chapter for n in state["needs_revalidation"]), "前文改动后，必须先核对更早受影响章节。")
    if chapter <= last:
        return
    review = review_status(state)
    require(not state["needs_revalidation"], "旧章改动后，后续章节尚未重新核对。")
    require(not review["window_review_due"], "已到近期回看点；先完成实际正文回看。")
    require(not review["unresolved_reviews"], "阶段回看有未解决的重要问题；先修复或作出作者决定。")
    if last and state["chapters"][str(last)]["stage"] != stage:
        valid = [c for c in state["checkpoints"] if valid_checkpoint(state, c)]
        previous_stage = state["chapters"][str(last)]["stage"]
        stage_start = last
        while stage_start > 1 and state["chapters"][str(stage_start - 1)]["stage"] == previous_stage:
            stage_start -= 1
        require(any(c["kind"] == "stage" and c["from"] <= stage_start and c["to"] == last
                    and c["decision"] in ("continue", "adjust_future") for c in valid),
                "进入新阶段前，需要覆盖上一阶段的完整回看。")


def adopt(root, text_path, record_path, revalidate=False):
    text = Path(text_path).read_text(encoding="utf-8")
    record = read_json(record_path)
    with writer_lock(root):
        state = load_state(root)
        verify_files(root, state)
        prior = state["chapters"].get(str(record.get("chapter")))
        if not revalidate and prior:
            stored = read_json(inside(root, prior["record_path"]))
            if stored == dict(record, measured_han=han_count(text)) and prior["text_sha256"] == digest(text):
                return {"chapter": record["chapter"], "revision": state["revision"], "already_adopted": True}
        serial = state.get("serialization", {}).get("enabled")
        if serial:
            serial_module().guard_chapter(state, record.get("chapter"), serial_api())
        count = validate_record(root, state, text, record)
        chapter = record["chapter"]
        last = len(state["chapters"])
        if revalidate:
            require(chapter in state["needs_revalidation"], "该章没有待重新核对标记。")
            require(state["chapters"][str(chapter)]["text_sha256"] == digest(text), "重核模式不能更改正文；改稿请用 adopt。")
            require(state["tasks"].get(str(chapter), {}).get("phase") != "candidate", "已有改稿候选，应先处理该任务再重核。")
        else:
            task = state["tasks"].get(str(chapter), {})
            require(task.get("phase") == "candidate" and task.get("candidate_sha256") == digest(text), "先用candidate登记本稿；改稿使用repair计入同一任务。")
        ensure_next_allowed(state, chapter, record["stage"])
        release = serial_module().validated_gate(root, state, text, record, serial_api()) if serial else None
        old = state["chapters"].get(str(chapter))
        sha = digest(text)
        require(not old or old["stage"] == record["stage"], "已采用章的阶段归属不能由写后记录静默改变。")
        text_relative = f"chapters/CH{chapter:03d}_{sha}.txt"
        text_target = inside(root, text_relative)
        text_target.parent.mkdir(parents=True, exist_ok=True)
        immutable_text(text_target, text)
        canonical = dict(record, measured_han=count)
        encoded = json.dumps(canonical, ensure_ascii=False, indent=2) + "\n"
        record_relative = f"records/CH{chapter:03d}_{digest(encoded)}.json"
        record_target = inside(root, record_relative)
        record_target.parent.mkdir(parents=True, exist_ok=True)
        immutable_text(record_target, encoded)
        item = {"title": record["title"], "stage": record["stage"], "text_path": text_relative,
                "text_sha256": sha, "record_path": record_relative,
                "record_sha256": digest(encoded), "han_count": count}
        state["chapters"][str(chapter)] = item
        pending = set(state["needs_revalidation"])
        pending.discard(chapter)
        # 语义记录改变也可能改变后文依据，保守地标记后续，不自动改写。
        changed = old is not None and (old["text_sha256"] != sha or old["record_sha256"] != item["record_sha256"])
        if changed and not revalidate:
            pending.update(range(chapter + 1, last + 1))
        state["needs_revalidation"] = sorted(pending)
        if str(chapter) in state["tasks"]:
            state["tasks"][str(chapter)]["phase"] = "adopted"
        state["completion"] = None
        if serial:
            serial_module().on_adopt(state, chapter, item, release, serial_api())
        state["revision"] += 1
        atomic_json(Path(root) / "state.json", state)
        return {"chapter": chapter, "revision": state["revision"], "han_count": count,
                "text_path": text_relative, "needs_revalidation": state["needs_revalidation"]}


def checkpoint(root, report_path):
    report = read_json(report_path)
    with writer_lock(root):
        state = load_state(root)
        verify_files(root, state)
        require(report.get("base_revision") == state["revision"], "回看基于旧进度。")
        require(report.get("canon_sha256") == state["inputs"].get("canon", {}).get("sha256"), "回看所依据的作者设定已改变。")
        start, end = report.get("from"), report.get("to")
        require(isinstance(start, int) and isinstance(end, int) and 1 <= start <= end <= len(state["chapters"]), "回看范围无效。")
        require(report.get("kind") in ("window", "stage"), "回看类型无效。")
        require(report.get("read_mode") == "full_text_batches", "摘要检查不能标记为完成全文回看。")
        require(set(report.get("coverage", {})) == {str(n) for n in range(start, end + 1)}, "回看缺少章节或包含范围外章节。")
        require(set(report.get("record_coverage", {})) == set(report["coverage"]), "回看缺少对应写后记录版本。")
        require(valid_checkpoint(state, report), "回看的正文版本已经改变。")
        require(not any(start <= n <= end for n in state["needs_revalidation"]), "回看范围内仍有章节待重新核对。")
        require(report.get("decision") in ("continue", "adjust_future", "repair_required", "author_decision"), "缺少回看决定。")
        for field in ("conclusion", "causal_progress", "repetition_comparison", "character_changes", "promise_payoffs", "ending_runway", "next_action"):
            require(bool(report.get(field, "").strip()), f"回看缺少字段：{field}。")
        require(isinstance(report.get("findings"), list), "回看问题必须为列表。")
        if report["decision"] in ("repair_required", "author_decision"):
            require(bool(report["findings"]), "阻断决定必须列出具体问题。")
        state["checkpoints"].append(report)
        state["revision"] += 1
        atomic_json(Path(root) / "state.json", state)
        return {"revision": state["revision"], **review_status(state)}


def context(root, query="", budget=80000, story_time=None):
    state = load_state(root)
    verify_files(root, state)
    require(not state["needs_revalidation"], "前文变更后的影响核对未完成，不能组装正常续写包。")
    items = records(root, state)
    parts = ["# 本次续写资料包", json.dumps(status(root), ensure_ascii=False, indent=2),
             "说明：这是待阅读资料，不证明GPT已读完。字数预算按字符计，不是token预算。"]
    for kind, item in state["inputs"].items():
        parts.append(f"## {kind} {item['sha256']}\n" + inside(root, item["path"]).read_text(encoding="utf-8"))
    if items:
        last = len(items)
        adopted = state["chapters"][str(last)]
        parts.append(f"## 上一章全文 CH{last:03d} {adopted['text_sha256']}\n" + inside(root, adopted["text_path"]).read_text(encoding="utf-8"))
    parts.append("## 最近五章摘要与实际推进\n" + json.dumps(
        [{"chapter": n, "summary": r["summary"], "progression": r["progression"]} for n, r in items[-5:]], ensure_ascii=False, indent=2))
    projection = memory(root, story_time)
    terms = [t.lower() for t in re.split(r"[\s,，;；]+", query.strip()) if t]
    facts = projection["facts"]
    def score(fact):
        haystack = (fact["key"] + " " + fact["value"]).lower()
        return sum(term in haystack for term in terms), fact["known_at_chapter"]
    ranked = sorted(facts, key=score, reverse=True)
    selected = [f for f in ranked if not terms or score(f)[0] > 0][:30]
    optional = "## 关键词召回事实\n" + json.dumps(selected, ensure_ascii=False, indent=2)
    # 所有冲突条目必须放入必要区，不能因检索排序消失。
    parts.append("## 记忆冲突（先核对，不自动选真）\n" + json.dumps(projection["conflicts"], ensure_ascii=False, indent=2))
    parts.append("## 未闭合伏笔事件\n" + json.dumps(projection["open_threads"], ensure_ascii=False, indent=2))
    if state.get("serialization", {}).get("enabled"):
        parts.append("## 连载冻结与前向处理\n" + json.dumps(serial_module().continuation_state(root, state, serial_api()), ensure_ascii=False, indent=2))
    required = "\n\n".join(parts)
    require(len(required) + 300 <= budget, "必要资料超过字符预算；不能静默截断。请扩大预算，或由GPT明确记录分批读取范围。")
    omitted = len(facts) - len(selected)
    if len(required) + len(optional) + 300 <= budget:
        result = required + "\n\n" + optional
    else:
        result = required + "\n\n召回资料未放入本包：剩余预算不足，请另行搜索记录及原文。"
        omitted = len(facts)
    require(load_state(root)["revision"] == state["revision"], "组装资料期间正式版本改变，请重新读取。")
    return result + f"\n\n事实当前值共 {len(facts)} 条；本包未选入 {omitted} 条。关键词召回可能漏检，不能替代关键历史原文回查。\n"


def finish(root, report_path):
    report = read_json(report_path)
    with writer_lock(root):
        state = load_state(root); verify_files(root, state)
        require(report.get("base_revision") == state["revision"], "完稿审查基于旧进度。")
        require(len(state["chapters"]) == state["config"]["target_chapters"], "尚未达到目标章数。")
        require(not state["needs_revalidation"], "仍有章节待重核。")
        require(not any(t["phase"] == "candidate" for t in state["tasks"].values()), "仍有未处理候选任务。")
        review = review_status(state)
        require(review["reviewed_until"] == len(state["chapters"]) and not review["unresolved_reviews"], "全文回看未完整覆盖或仍有重要问题。")
        last = len(state["chapters"]); stage = state["chapters"][str(last)]["stage"]; start = last
        while start > 1 and state["chapters"][str(start - 1)]["stage"] == stage: start -= 1
        require(any(c["kind"] == "stage" and c["from"] <= start and c["to"] == last and c["decision"] in ("continue", "adjust_future") and valid_checkpoint(state, c) for c in state["checkpoints"]), "最后阶段尚未完成全文回看。")
        total = sum(c["han_count"] for c in state["chapters"].values())
        require(state["config"]["book_han_min"] <= total <= state["config"]["book_han_max"], "全书实测汉字数不在约定范围内。")
        require(report.get("decision") == "complete" and report.get("blocking_issues") == [], "完稿仍有必须处理的问题。")
        if state.get("serialization", {}).get("enabled"):
            serial_module().finish_guard(root, state, serial_api())
        for field in ("main_conflict", "character_choice", "promise_payoffs", "remaining_open_questions"):
            require(bool(report.get(field, "").strip()), f"完稿报告缺少{field}。")
        require(isinstance(report.get("evidence"), list) and bool(report["evidence"]), "完稿判断缺少正文证据。")
        for source in report["evidence"]:
            item = state["chapters"].get(str(source.get("chapter")))
            require(item is not None and source.get("sha256") == item["text_sha256"], "完稿证据版本无效。")
            require(quote_exists(inside(root, item["text_path"]).read_text(encoding="utf-8"), source.get("quote")), "完稿证据引文不存在。")
        state["completion"] = report
        state["revision"] += 1
        atomic_json(Path(root) / "state.json", state)
        return {"completed": True, "actual_han": total, "revision": state["revision"], "note": "程序未评价文学质量，也未证明已保存至远程。"}


def reopen(root, reason):
    require(bool(reason.strip()), "需要记录作者重开修改的原因。")
    with writer_lock(root):
        state = load_state(root); verify_files(root, state)
        require(state["completion"] is not None, "当前未完稿，无需重开。")
        state.setdefault("completion_history", []).append(dict(state["completion"], reopen_reason=reason))
        state["completion"] = None; state["revision"] += 1
        atomic_json(Path(root) / "state.json", state)
        return {"revision": state["revision"], "completed": False}


def export_text(root, out):
    state = load_state(root); verify_files(root, state)
    require(not state["needs_revalidation"], "旧章改动尚未核对，不能导出为有效正文。")
    require(bool(state["chapters"]), "尚无采用正文。")
    target = Path(out).resolve(); require(not target.is_relative_to(Path(root).resolve()), "导出到作品目录外，避免覆盖来源。")
    parts = [f"第{n}章 {state['chapters'][str(n)]['title']}\n\n" + inside(root, state["chapters"][str(n)]["text_path"]).read_text(encoding="utf-8") for n in range(1, len(state["chapters"]) + 1)]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n\n".join(parts), encoding="utf-8")
    return {"path": str(target), "chapters": len(parts), "completed": state["completion"] is not None}


def verify_snapshot(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "快照存在重复文件名。")
        for name in names:
            require(not Path(name).is_absolute() and ".." not in Path(name).parts and "\\" not in name, "快照包含不安全路径。")
        manifest = json.loads(archive.read("SNAPSHOT_MANIFEST.json"))
        require(set(manifest["files"]) == set(names) - {"SNAPSHOT_MANIFEST.json"}, "快照文件清单不完整。")
        for name, sha in manifest["files"].items():
            require(hashlib.sha256(archive.read(name)).hexdigest() == sha, f"快照文件损坏：{name}")
        state = json.loads(archive.read("state.json"))
        require(manifest["book_id"] == state["book_id"] and manifest["revision"] == state["revision"], "快照身份或版本不符。")
        return {"valid": True, "book_id": state["book_id"], "revision": state["revision"], "files": len(manifest["files"]), "note": "完整性校验不是文学质量或文件来源的认证。"}


def snapshot(root, out):
    root = Path(root).resolve(); out = Path(out).resolve()
    require(not out.is_relative_to(root), "快照必须保存在作品目录外。")
    out.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock(root):
        state = load_state(root); verify_files(root, state)
        files = {}
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != ".writer-lock" and not path.name.startswith(".pending-"):
                require(path.resolve().is_relative_to(root), "快照不得包含工作区外的符号链接。")
                files[path.relative_to(root).as_posix()] = path.read_bytes()
        # 快照同时携带本工具和关键规程，恢复不依赖原临时目录。
        package = Path(__file__).resolve().parents[1]
        files["SYSTEM/tools/novel_state.py"] = Path(__file__).read_bytes()
        for name in ("启动指令.txt", "启动新书.txt", "每日一章指令.txt", "docs/连载冻结与发布前检查.md", "平台研究与选题.md", "platforms.json", "执行规程.md", "工具使用与数据格式.md", "system_config.json", "project_config.json"):
            path = package / name
            if path.is_file(): files["SYSTEM/" + name] = path.read_bytes()
        for name in ("online_state.py", "make_init_package.py", "serial_release.py"):
            path = package / "tools" / name
            if path.is_file(): files["SYSTEM/tools/" + name] = path.read_bytes()
        templates = package / "templates"
        if templates.is_dir():
            for path in sorted(templates.iterdir()):
                if path.is_file(): files["SYSTEM/templates/" + path.name] = path.read_bytes()
        manifest = {"book_id": state["book_id"], "revision": state["revision"], "files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}
        fd, temp = tempfile.mkstemp(prefix=".snapshot-", dir=out.parent); os.close(fd)
        try:
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in files.items(): archive.writestr(name, data)
                archive.writestr("SNAPSHOT_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            verify_snapshot(temp)
            os.replace(temp, out)
        finally:
            if os.path.exists(temp): os.unlink(temp)
    return dict(verify_snapshot(out), path=str(out), sha256=hashlib.sha256(out.read_bytes()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="book")
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("init"); p.add_argument("--title", required=True); p.add_argument("--config")
    subs.add_parser("status")
    p = subs.add_parser("count"); p.add_argument("text")
    for command in ("adopt", "revalidate", "check-record"):
        p = subs.add_parser(command); p.add_argument("--text", required=True); p.add_argument("--record", required=True)
    p = subs.add_parser("checkpoint"); p.add_argument("--report", required=True)
    p = subs.add_parser("context"); p.add_argument("--query", default=""); p.add_argument("--budget", type=int, default=80000); p.add_argument("--out")
    p.add_argument("--time", type=int)
    p = subs.add_parser("set-input"); p.add_argument("--kind", choices=("canon", "roadmap"), required=True); p.add_argument("--file", required=True); p.add_argument("--reason", required=True); p.add_argument("--base-revision", type=int, required=True)
    for command in ("candidate", "repair"):
        p = subs.add_parser(command); p.add_argument("--chapter", type=int, required=True); p.add_argument("--text", required=True); p.add_argument("--mode", choices=("write", "edit"), default="write"); p.add_argument("--reason", default="")
    p = subs.add_parser("memory"); p.add_argument("--time", type=int); p.add_argument("--through", type=int)
    p = subs.add_parser("read-chapter"); p.add_argument("--chapter", type=int, required=True)
    p = subs.add_parser("finish"); p.add_argument("--report", required=True)
    p = subs.add_parser("reopen"); p.add_argument("--reason", required=True)
    for command in ("snapshot", "export"):
        p = subs.add_parser(command); p.add_argument("--out", required=True)
    p = subs.add_parser("verify-snapshot"); p.add_argument("--file", required=True)
    p = subs.add_parser("serial-enable"); p.add_argument("--reason", required=True); p.add_argument("--base-revision", required=True, type=int)
    for command in ("release-request", "release-check"):
        p = subs.add_parser(command); p.add_argument("--text", required=True); p.add_argument("--record", required=True)
        if command == "release-check": p.add_argument("--report", required=True)
        else: p.add_argument("--out", required=True)
    p = subs.add_parser("publish"); p.add_argument("--chapter", required=True, type=int); p.add_argument("--text-sha256", required=True); p.add_argument("--published-at", required=True); p.add_argument("--evidence", required=True)
    p = subs.add_parser("incident"); p.add_argument("--report", required=True)
    p = subs.add_parser("serial-plan"); p.add_argument("--report", required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = init(args.root, args.title, read_json(args.config) if args.config else None)
        elif args.command == "status":
            result = status(args.root)
        elif args.command == "count":
            text = Path(args.text).read_text(encoding="utf-8")
            result = {"sha256": digest(text), "han_count": han_count(text), "all_characters": len(text)}
        elif args.command in ("adopt", "revalidate"):
            result = adopt(args.root, args.text, args.record, revalidate=args.command == "revalidate")
        elif args.command == "check-record":
            state = load_state(args.root); verify_files(args.root, state)
            result = {"valid_mechanically": True, "han_count": validate_record(args.root, state, Path(args.text).read_text(encoding="utf-8"), read_json(args.record)), "note": "未验证语义判断及实际阅读行为"}
        elif args.command == "checkpoint":
            result = checkpoint(args.root, args.report)
        elif args.command == "set-input":
            result = set_input(args.root, args.kind, args.file, args.reason, args.base_revision)
        elif args.command in ("candidate", "repair"):
            result = candidate(args.root, args.chapter, args.text, args.mode, args.reason, args.command == "repair")
        elif args.command == "memory":
            result = memory(args.root, args.time, args.through)
        elif args.command == "read-chapter":
            state = load_state(args.root); verify_files(args.root, state)
            item = state["chapters"].get(str(args.chapter)); require(item is not None, "章节不存在。")
            result = {"chapter": args.chapter, "needs_revalidation": args.chapter in state["needs_revalidation"], "text": inside(args.root, item["text_path"]).read_text(encoding="utf-8"), "record": read_json(inside(args.root, item["record_path"]))}
        elif args.command == "finish":
            result = finish(args.root, args.report)
        elif args.command == "reopen":
            result = reopen(args.root, args.reason)
        elif args.command == "snapshot":
            result = snapshot(args.root, args.out)
        elif args.command == "verify-snapshot":
            result = verify_snapshot(args.file)
        elif args.command == "export":
            result = export_text(args.root, args.out)
        elif args.command == "serial-enable":
            result = serial_module().enable(args.root, args.reason, args.base_revision, serial_api())
        elif args.command == "release-request":
            result = serial_module().request(args.root, args.text, args.record, args.out, serial_api())
        elif args.command == "release-check":
            result = serial_module().check(args.root, args.text, args.record, args.report, serial_api())
        elif args.command == "publish":
            result = serial_module().publish(args.root, args.chapter, args.text_sha256, args.published_at, args.evidence, serial_api())
        elif args.command == "incident":
            result = serial_module().incident(args.root, args.report, serial_api())
        elif args.command == "serial-plan":
            result = serial_module().plan(args.root, args.report, serial_api())
        else:
            result = context(args.root, args.query, args.budget, args.time)
            if args.out:
                Path(args.out).write_text(result, encoding="utf-8")
                result = {"path": str(Path(args.out).resolve()), "characters": len(result)}
        print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
    except (WorkspaceError, OSError, KeyError, TypeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        parser.exit(2, f"未完成：{exc}\n")


if __name__ == "__main__":
    main()
