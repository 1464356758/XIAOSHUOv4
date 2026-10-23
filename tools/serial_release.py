"""连载定稿边界。校验来源与流程；文学判断由实际阅读正文的GPT和作者负责。"""
from datetime import datetime
import json
from pathlib import Path

CHECKS = ("timeline", "knowledge", "props_resources", "causality", "power_rules",
          "promises", "anti_repeat", "character_agency")
CONCLUSIONS = ("conclusion", "causal_progress", "repetition_comparison", "character_changes",
               "promise_payoffs", "ending_runway", "next_action")
DEFAULTS = {"serial_stage_size": 10, "serial_lookahead": 3,
            "opening_hook_max_han": 800, "opening_core_max_han": 3000,
            "opening_impact_max_han": 3000}


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def new_state(config, a):
    for key, default in DEFAULTS.items():
        config.setdefault(key, default)
        a.require(type(config[key]) is int and config[key] > 0, f"{key}必须为正整数。")
    a.require(1 <= config["serial_lookahead"] <= 5, "近期路线窗口须为1—5章。")
    return {"schema_version": 1, "enabled": True, "frozen": {}, "published": {},
            "gates": {}, "promises": {}, "incidents": {}}


def verify_frozen(state, a):
    serial = state["serialization"]
    a.require(state["config"].get("serial_mode") is True, "冻结项目不能静默关闭连载模式。")
    a.require(set(serial["frozen"]) == set(state["chapters"]), "采用章与冻结清单不一致。")
    for n, pin in serial["frozen"].items():
        item = state["chapters"][n]
        a.require(all(pin[k] == item[k] for k in ("text_sha256", "record_sha256")),
                  f"第{n}章定稿正文或事实记录已冻结，不能替换版本。")


def guard_chapter(state, chapter, a):
    a.require(str(chapter) not in state["serialization"]["frozen"],
              "本章定稿已冻结；不能edit、repair或revalidate。旧错登记incident，只处理未定稿内容。")


def events(root, state, a, record=None):
    result = {}
    for n, rec in a.records(root, state):
        for e in rec["thread_events"]:
            result[e["id"]] = dict(e, source_chapter=n)
    if record:
        for e in record["thread_events"]:
            result[e["id"]] = dict(e, source_chapter=record["chapter"])
    return result


def enable(root, reason, base_revision, a):
    with a.writer_lock(root):
        state = a.load_state(root); a.verify_files(root, state)
        a.require(base_revision == state["revision"] and nonempty(reason), "冻结迁移基于旧版本或缺少原因。")
        if state.get("serialization", {}).get("enabled"):
            return dict(summary(state), unchanged=True, revision=state["revision"])
        a.require(not state["needs_revalidation"] and
                  not any(t["phase"] == "candidate" for t in state["tasks"].values()),
                  "先处理候选和待重核章节，再冻结现行版本。")
        review = a.review_status(state)
        a.require(not review["window_review_due"] and not review["unresolved_reviews"], "先完成到期回看并处理阻断结论，再建立冻结基线。")
        state["config"]["serial_mode"] = True
        serial = state["serialization"] = new_state(state["config"], a)
        serial["migration"] = {"reason": reason, "base_revision": base_revision,
                               "note": "既有采用版本冻结；不追认开篇检查或平台发布。"}
        for n, item in state["chapters"].items():
            serial["frozen"][n] = {k: item[k] for k in ("text_sha256", "record_sha256")}
        for key, e in events(root, state, a).items():
            if e["action"] not in ("payoff", "close"):
                serial["promises"][key] = {"deadline_chapter": None, "status": "open", "history": []}
        state["revision"] += 1; a.atomic_json(Path(root) / "state.json", state)
        return dict(summary(state), revision=state["revision"])


def canonical_sha(text, record, a):
    return a.digest(json.dumps(dict(record, measured_han=a.han_count(text)), ensure_ascii=False, indent=2) + "\n")


def bindings(state, text, record, a):
    n = record["chapter"]; task = state["tasks"].get(str(n), {})
    a.require(state.get("serialization", {}).get("enabled"), "本书尚未启用连载冻结。")
    guard_chapter(state, n, a)
    a.require(n == len(state["chapters"]) + 1, "发布前检查只接受下一章。")
    a.require(task.get("phase") == "candidate" and task.get("candidate_sha256") == a.digest(text),
              "先登记当前候选，再生成检查请求。")
    return {"schema_version": 1, "book_id": state["book_id"], "chapter": n,
            "base_revision": state["revision"], "task_id": task["task_id"], "repairs": task["repairs"],
            "text_sha256": a.digest(text), "record_sha256": canonical_sha(text, record, a),
            "canon_sha256": state["inputs"]["canon"]["sha256"],
            "roadmap_sha256": state["inputs"]["roadmap"]["sha256"]}


def checkpoint_scope(state, record):
    n = record["chapter"]; c = state["config"]
    if n % c["serial_stage_size"] == 0 or n == c["target_chapters"] or record.get("stage_closing") is True:
        start = n
        while start > 1 and state["chapters"][str(start - 1)]["stage"] == record["stage"]:
            start -= 1
        return "stage", start, n
    if n % c["review_interval"] == 0:
        return "window", max(1, n - c["review_interval"] + 1), n
    return None


def source_map(state, record, a):
    n = record["chapter"]
    texts = {k: item["text_sha256"] for k, item in state["chapters"].items()}
    recs = {k: item["record_sha256"] for k, item in state["chapters"].items()}
    texts[str(n)] = record["text_sha256"]
    return texts, recs


def evidence(root, state, text, n, item, a, current_only=False):
    a.require(isinstance(item, dict), "证据必须包含章节、版本和原文。")
    chapter = item.get("chapter")
    a.require(type(chapter) is int and 1 <= chapter <= n and (not current_only or chapter == n), "证据章号无效。")
    body = text if chapter == n else a.inside(root, state["chapters"][str(chapter)]["text_path"]).read_text(encoding="utf-8")
    a.require(item.get("sha256") == a.digest(body) and a.quote_exists(body, item.get("quote")),
              "证据版本已变或引文不在正文内。")


def skeleton(root, state, text, record, a):
    b = bindings(state, text, record, a); n = record["chapter"]
    texts, recs = source_map(state, record, a); recs[str(n)] = b["record_sha256"]
    ev = {"chapter": n, "sha256": b["text_sha256"], "quote": ""}
    report = dict(b, decision="pending", read_full_text=False, blocking_issues=["尚未实际阅读检查"],
                  read_coverage={str(k): texts[str(k)] for k in range(max(1, n - 3), n + 1)},
                  reader_reason_to_continue="", consequence="", ending_runway="",
                  checks={key: {"decision": "pending", "conclusion": "", "evidence": [dict(ev)]} for key in CHECKS},
                  comparison=[{"chapter": k, "sha256": texts[str(k)], "quote": "", "difference": ""}
                              for k in range(max(1, n - 3), n)],
                  future_window=[{"chapter": k, "goal": "", "change": "", "ending_question": ""}
                                 for k in range(n + 1, min(state["config"]["target_chapters"], n + state["config"]["serial_lookahead"]) + 1)],
                  promise_reviews=[], incident_actions=[])
    old = events(root, state, a); new = events(root, state, a, record)
    relevant = {k for k, e in old.items() if e["action"] not in ("payoff", "close")}
    relevant.update(e["id"] for e in record["thread_events"])
    for key in sorted(relevant):
        plan = state["serialization"]["promises"].get(key, {})
        done = new[key]["action"] in ("payoff", "close")
        report["promise_reviews"].append({"id": key, "action": "complete" if done else "keep" if plan.get("deadline_chapter") else "set",
                                          "deadline_chapter": plan.get("deadline_chapter"), "reason": "", "evidence": dict(ev)})
    for key, incident in state["serialization"]["incidents"].items():
        if incident["status"] == "open":
            report["incident_actions"].append({"id": key, "action": "pending", "explanation": "", "evidence": dict(ev)})
    if n == 1:
        report["opening"] = {key: {"quote": "", "why": ""} for key in ("first_hook", "core_promise", "protagonist_impact")}
    scope = checkpoint_scope(state, record)
    if scope:
        kind, start, end = scope
        report["checkpoint"] = {"kind": kind, "from": start, "to": end, "base_revision": b["base_revision"],
                                "canon_sha256": b["canon_sha256"], "read_mode": "pending", "decision": "pending",
                                "coverage": {str(k): texts[str(k)] for k in range(start, end + 1)},
                                "record_coverage": {str(k): recs[str(k)] for k in range(start, end + 1)},
                                "findings": [], **{key: "" for key in CONCLUSIONS}}
    return report


def request(root, text_path, record_path, out, a):
    state = a.load_state(root); a.verify_files(root, state)
    text = Path(text_path).read_text(encoding="utf-8"); record = a.read_json(record_path)
    a.validate_record(root, state, text, record)
    report = skeleton(root, state, text, record, a)
    target = Path(out).resolve()
    a.require(not target.exists(), "检查请求已存在；保留人工填写内容，改用新路径。")
    a.require(not target.is_relative_to(Path(root).resolve()), "检查请求先写到作品目录外，审完再登记不可变报告。")
    a.atomic_json(target, report)
    return {"path": str(target), "decision": "pending", "note": "必须实际阅读全文并填写证据；模板不是通过报告。"}


def validate(root, state, text, record, report, a):
    a.validate_record(root, state, text, record)
    b = bindings(state, text, record, a); n = record["chapter"]
    a.require(all(report.get(k) == v for k, v in b.items()), "发布前检查已过期或不对应当前正文、记录、候选、设定及路线。")
    a.require(report.get("decision") == "release" and report.get("read_full_text") is True and report.get("blocking_issues") == [], "发布前仍有阻断项或未实际阅读。")
    expected = skeleton(root, state, text, record, a)
    a.require(report.get("read_coverage") == expected["read_coverage"], "缺少本章及最近三章的完整阅读覆盖。")
    for key in ("reader_reason_to_continue", "consequence", "ending_runway"):
        a.require(nonempty(report.get(key)), f"缺少{key}具体结论。")
    for key in CHECKS:
        check = report.get("checks", {}).get(key, {})
        a.require(check.get("decision") == "pass" and nonempty(check.get("conclusion")) and
                  isinstance(check.get("evidence"), list) and check["evidence"], f"缺少{key}证据检查。")
        for ev in check["evidence"]: evidence(root, state, text, n, ev, a)
    comparison = report.get("comparison", [])
    a.require([x.get("chapter") for x in comparison] == [x["chapter"] for x in expected["comparison"]], "反重复比较缺少最近三章。")
    for item in comparison:
        evidence(root, state, text, n, item, a)
        a.require(nonempty(item.get("difference")), "不能只抄旧摘要，需说明办法、选择及后果的实际差异。")
    future = report.get("future_window", [])
    a.require([x.get("chapter") for x in future] == [x["chapter"] for x in expected["future_window"]], "缺少近期路线及结果。")
    for item in future:
        a.require(all(nonempty(item.get(k)) for k in ("goal", "change", "ending_question")), "近期路线不能只列章节名。")
    if n == 1:
        for key, limit in (("first_hook", "opening_hook_max_han"), ("core_promise", "opening_core_max_han"), ("protagonist_impact", "opening_impact_max_han")):
            cue = report.get("opening", {}).get(key, {}); quote = cue.get("quote")
            a.require(a.quote_exists(text, quote) and nonempty(cue.get("why")), "开篇须给出真实吸引点、题材承诺与主角受影响的原文。")
            end = a.han_count(text[:text.index(quote) + len(quote)])
            a.require(end <= state["config"][limit], f"开篇{key}出现在第{end}汉字，超过本书{state['config'][limit]}字检查线；定稿前调整。")
    scope = checkpoint_scope(state, record)
    if scope:
        cp = report.get("checkpoint", {}); target = expected["checkpoint"]
        for key in ("kind", "from", "to", "base_revision", "canon_sha256", "coverage", "record_coverage"):
            a.require(cp.get(key) == target[key], "到期回看必须在定稿前覆盖旧章及当前候选的准确正文、记录版本。")
        a.require(cp.get("read_mode") == "full_text_batches" and cp.get("decision") in ("continue", "adjust_future") and
                  isinstance(cp.get("findings"), list) and all(nonempty(cp.get(k)) for k in CONCLUSIONS), "回看未完成或尚需修复；不能先定稿。")
    reviews = report.get("promise_reviews", [])
    a.require(len({x.get("id") for x in reviews}) == len(reviews) and
              {x.get("id") for x in reviews} == {x["id"] for x in expected["promise_reviews"]}, "必须逐项处理开放承诺，不能遗漏或重复。")
    current = events(root, state, a, record)
    for item in reviews:
        key = item["id"]; old = state["serialization"]["promises"].get(key, {}); deadline = item.get("deadline_chapter")
        a.require(nonempty(item.get("reason")), "承诺复核须说明当前状态及到期安排。")
        if current[key]["action"] in ("payoff", "close"):
            a.require(item.get("action") == "complete", "兑现状态须与实际线索事件一致。")
            evidence(root, state, text, n, item.get("evidence"), a, current_only=True)
        else:
            a.require(type(deadline) is int and n <= deadline <= state["config"]["target_chapters"], "开放承诺须有未过期且不超全书的到期章。")
            previous = old.get("deadline_chapter")
            if previous is None:
                a.require(item.get("action") == "set", "首次为承诺设定明确到期章。")
            elif deadline == previous:
                a.require(item.get("action") == "keep", "推进或部分兑现不能悄悄重置到期章。")
            else:
                a.require(item.get("action") in ("reschedule", "miss") and nonempty(item.get("cost")), "延期须有实际原因、代价和新期限，不能无限换答案。")
                a.require((item["action"] == "miss") == (n >= previous), "如实区分到期失约和提前调整。")
                evidence(root, state, text, n, item.get("evidence"), a, current_only=True)
    actions = report.get("incident_actions", [])
    a.require(len({x.get("id") for x in actions}) == len(actions) and {x.get("id") for x in actions} == {x["id"] for x in expected["incident_actions"]}, "旧错前向处理不可遗漏或假装已修。")
    for item in actions:
        a.require(item.get("action") in ("contain", "resolve") and nonempty(item.get("explanation")), "旧错需落实本章处置并保留历史代价。")
        evidence(root, state, text, n, item.get("evidence"), a, current_only=True)
        if item["action"] == "contain":
            a.require(type(item.get("next_chapter")) is int and n < item["next_chapter"] <= state["config"]["target_chapters"], "未解决问题须有下一处理章。")
    return report


def check(root, text_path, record_path, report_path, a):
    with a.writer_lock(root):
        state = a.load_state(root); a.verify_files(root, state)
        text = Path(text_path).read_text(encoding="utf-8"); record = a.read_json(record_path)
        report = validate(root, state, text, record, a.read_json(report_path), a)
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"; sha = a.digest(encoded)
        relative = f"releases/CH{record['chapter']:03d}_{sha}.json"
        a.immutable_text(a.inside(root, relative), encoded)
        state["serialization"]["gates"][str(record["chapter"])] = {"path": relative, "sha256": sha, "used": False}
        a.atomic_json(Path(root) / "state.json", state)
        return {"valid_mechanically": True, "path": relative, "note": "可执行采用；不证明文学质量、实际阅读行为或平台发布。"}


def validated_gate(root, state, text, record, a):
    gate = state["serialization"]["gates"].get(str(record["chapter"]), {})
    a.require(gate and not gate["used"], "缺少当前发布前检查；先release-request、实际审阅，再release-check。")
    body = a.inside(root, gate["path"]).read_text(encoding="utf-8")
    a.require(a.digest(body) == gate["sha256"], "发布前检查报告已被改写。")
    return validate(root, state, text, record, json.loads(body), a)


def on_adopt(state, chapter, item, report, a):
    serial = state["serialization"]
    serial["frozen"][str(chapter)] = {k: item[k] for k in ("text_sha256", "record_sha256")}
    serial["gates"][str(chapter)]["used"] = True
    if "checkpoint" in report:
        state["checkpoints"].append(report["checkpoint"])
    for review in report["promise_reviews"]:
        plan = serial["promises"].setdefault(review["id"], {"history": []})
        plan.update(deadline_chapter=review.get("deadline_chapter"), status="closed" if review["action"] == "complete" else "open")
        plan["last_reviewed_chapter"] = chapter
        if review["action"] != "keep":
            plan["history"].append(dict(review, chapter=chapter))
    for action in report["incident_actions"]:
        incident = serial["incidents"][action["id"]]
        incident["history"].append(dict(action, chapter=chapter))
        if action["action"] == "resolve": incident["status"] = "resolved_forward"
        else: incident["target_chapter"] = action["next_chapter"]


def publish(root, chapter, sha, published_at, proof, a):
    try:
        stamp = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        a.require(False, "发布时间须为带时区的ISO日期。")
    a.require(stamp.tzinfo is not None and nonempty(proof), "必须有带时区的发布时间及真实发布依据。")
    a.require(stamp <= datetime.now(stamp.tzinfo), "未来预约时间不能登记为已发布。")
    with a.writer_lock(root):
        state = a.load_state(root); a.verify_files(root, state)
        serial = state.get("serialization", {}); pin = serial.get("frozen", {}).get(str(chapter))
        a.require(pin and pin["text_sha256"] == sha, "只能登记已冻结且哈希相同的定稿。")
        receipt = {"text_sha256": sha, "published_at": published_at, "evidence": proof}
        old = serial["published"].get(str(chapter))
        a.require(old is None or old == receipt, "发布回执不可覆盖；核对实际平台版本，保留原回执。")
        if old is None:
            serial["published"][str(chapter)] = receipt; state["revision"] += 1
            a.atomic_json(Path(root) / "state.json", state)
        return dict(summary(state), revision=state["revision"], unchanged=old is not None)


def incident(root, report_path, a):
    report = a.read_json(report_path)
    with a.writer_lock(root):
        state = a.load_state(root); a.verify_files(root, state)
        serial = state.get("serialization", {})
        a.require(serial.get("enabled") and report.get("base_revision") == state["revision"], "前向处理基于旧进度或尚未冻结。")
        a.require(all(nonempty(report.get(k)) for k in ("id", "problem", "impact", "forward_plan")), "旧错需有问题、影响及前向方案。")
        a.require(type(report.get("target_chapter")) is int and len(state["chapters"]) < report["target_chapter"] <= state["config"]["target_chapters"], "前向处置只能指向未定稿章节。")
        source = report.get("source", {}); n = source.get("chapter")
        a.require(str(n) in serial["frozen"], "问题须定位到冻结来源。")
        evidence(root, state, "", len(state["chapters"]) + 1, source, a)
        a.require(report["id"] not in serial["incidents"], "问题编号已存在，不能覆盖历史。")
        serial["incidents"][report["id"]] = dict(report, status="open", history=[])
        state["revision"] += 1; a.atomic_json(Path(root) / "state.json", state)
        return {"revision": state["revision"], "status": "open", "note": "只登记问题，未改写旧正文或事实。"}


def summary(state):
    serial = state["serialization"]
    return {"serial_mode": True, "frozen_chapters": len(serial["frozen"]),
            "published_chapters": len(serial["published"]),
            "publication_status": "只统计真实回执；无回执不等于未发布",
            "open_incidents": sum(x["status"] == "open" for x in serial["incidents"].values()),
            "release_check_required": True}


def continuation_state(root, state, a):
    # 历史报告留在来源文件中；必要上下文只放当前开放项，避免日更累积挤爆资料包。
    promises = {key: {k: value.get(k) for k in ("deadline_chapter", "status", "last_reviewed_chapter")}
                for key, value in state["serialization"]["promises"].items() if value["status"] == "open"}
    incidents = {key: {k: value.get(k) for k in ("source", "problem", "impact", "forward_plan", "target_chapter", "status")}
                 for key, value in state["serialization"]["incidents"].items() if value["status"] == "open"}
    return dict(summary(state), promises=promises, incidents=incidents,
                rule="定稿后正文与事实记录冻结；新错只登记前向处置。未定稿章先做发布前检查，五章及阶段全文回看前置，采用后才允许导出发布。")


def plan(root, report_path, a):
    """迁入时一次性给已有开放承诺补计划；不能覆盖已有期限。"""
    report = a.read_json(report_path)
    with a.writer_lock(root):
        state = a.load_state(root); a.verify_files(root, state)
        serial = state.get("serialization", {})
        a.require(serial.get("enabled") and report.get("base_revision") == state["revision"] and
                  report.get("roadmap_sha256") == state["inputs"].get("roadmap", {}).get("sha256"), "承诺计划基于旧进度或旧路线。")
        plans = report.get("plans", [])
        missing = {key for key, item in serial["promises"].items() if item["status"] == "open" and item["deadline_chapter"] is None}
        a.require(missing and len({x.get("id") for x in plans}) == len(plans) and {x.get("id") for x in plans} == missing,
                  "只能一次补齐全部未规划承诺；已有期限不能通过此入口覆盖。")
        for item in plans:
            n = item.get("deadline_chapter")
            a.require(type(n) is int and len(state["chapters"]) < n <= state["config"]["target_chapters"] and nonempty(item.get("reason")), "承诺计划须有明确的未来到期章与理由。")
            target = serial["promises"][item["id"]]
            target["deadline_chapter"] = n
            target["history"].append(dict(item, action="set", source="roadmap", roadmap_sha256=report["roadmap_sha256"], chapter=len(state["chapters"])))
        state["revision"] += 1; a.atomic_json(Path(root) / "state.json", state)
        return {"revision": state["revision"], "planned": len(plans), "note": "只登记未来期限，不新增已发生事实或兑现状态。"}


def finish_guard(root, state, a):
    a.require(not any(e["action"] not in ("payoff", "close") for e in events(root, state, a).values()), "完稿前逐项兑现或明确关闭开放承诺。")
    a.require(not any(x["status"] == "open" for x in state["serialization"]["incidents"].values()), "旧错前向处置尚未落实。")
