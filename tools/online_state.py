#!/usr/bin/env python3
"""GitHub小说工作区控制与阅读索引。不调用模型，不提交远程。"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

import novel_state as ns

def current_state(project):
    project = Path(project)
    if (project / 'book/private_storage.json').exists():
        import private_book
        pointer, envelope = private_book.read_public(project)
        public = envelope['public']
        return dict(public, chapters={str(x['number']): x for x in public['chapters']},
                    completion=True if public['progress']['completed'] else None,
                    needs_revalidation=public['progress']['needs_revalidation']), (pointer, envelope)
    state = ns.load_state(project / 'book')
    ns.verify_files(project / 'book', state)
    return state, None

PHASES = {
    "WAITING_PLATFORM": "等待选择平台", "RESEARCH_PLATFORM": "核查平台规则",
    "SCANNING_RANKINGS": "研究榜单", "WAITING_TOPIC": "等待选择题材",
    "PLANNING": "长篇规划", "WRITING": "撰写正文", "REVIEWING": "检查本章",
    "REVISING": "局部修订", "CHECKPOINT": "阶段回看", "SAVING": "保存进度",
    "BLOCKED": "需要处理问题", "WAITING_NEXT": "等待下一章", "COMPLETE": "全书完成",
    "SYSTEM_UPGRADE": "升级创作系统"
}

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def repository(value):
    value = value.strip()
    if not value.startswith("https://"): value = "https://github.com/" + value
    parsed = urlparse(value)
    ns.require(parsed.scheme == "https" and parsed.netloc.lower() == "github.com" and not parsed.query and not parsed.fragment, "请使用GitHub仓库链接。")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    ns.require(len(parts) == 2 and all(re.fullmatch(r"[A-Za-z0-9_.-]+", x) and x not in (".", "..") for x in parts), "链接应为https://github.com/用户名/仓库名。")
    return "https://github.com/" + "/".join(parts)

def get_config(project):
    return ns.read_json(Path(project) / "project_config.json")

def load_workflow(project):
    value = ns.read_json(Path(project) / "book/workflow.json")
    ns.require(value.get("schema_version") == 1, "不支持的工作流格式。")
    return value

def write_workflow(project, value, phase=None, detail=None, next_action=None):
    if phase is not None:
        ns.require(phase in PHASES, "任务阶段无效。")
        value["phase"] = phase
    if detail is not None: value["detail"] = detail.strip()[:2000]
    if next_action is not None: value["next_action"] = next_action.strip()[:1000]
    value["revision"] += 1; value["updated_at"] = now()
    value.setdefault("history", []).append({"at": value["updated_at"], "phase": value["phase"], "label": PHASES[value["phase"]], "detail": value.get("detail", "")})
    value["history"] = value["history"][-100:]
    ns.atomic_json(Path(project) / "book/workflow.json", value)

def bootstrap(project, repo_url, branch="main", title="未命名小说"):
    project = Path(project).resolve(); repo_url = repository(repo_url)
    ns.require(bool(branch.strip()) and len(branch) <= 200 and not any(x in branch for x in ("..", "\\", "?", "#")), "分支名称无效。")
    config = get_config(project)
    old = config.get("authority", {}).get("repository_url", "")
    ns.require(not old or repository(old) == repo_url, "已有其他作品身份，请使用空的初始化包建立新小说。")
    book = project / "book"
    existing = (book / "state.json").exists()
    if not existing:
        ns.init(book, title, ns.read_json(project / "system_config.json"))
    state = ns.load_state(book); ns.verify_files(book, state)
    config["system_version"] = "4.2-serial"
    config["project_name"] = state["title"]
    config["authority"] = {"mode": "github", "repository_url": repo_url, "branch": branch}
    ns.atomic_json(project / "project_config.json", config)
    if not (book / "workflow.json").exists():
        wf = {"schema_version": 1, "revision": -1, "book_id": state["book_id"], "phase": "WAITING_PLATFORM", "platform": None, "selected_topic": None, "research_path": None, "topics_path": None, "chapter": 1, "history": []}
        with ns.writer_lock(book):
            write_workflow(project, wf, detail="系统已准备好，请先选择创作平台。", next_action="在起点、番茄、七猫、晋江中选择一个平台，或指定其他平台。")
    build_index(project)
    return {"book_id": state["book_id"], "initialized": True, "already_initialized": existing, "phase": load_workflow(project)["phase"], "repository_url": repo_url}

def catalog(project):
    return {x["id"]: x for x in ns.read_json(Path(project) / "platforms.json")["platforms"]}

def select_platform(project, platform):
    ns.require(platform in catalog(project), "请使用平台目录中的编号。其他平台先由GPT补充其官方来源。")
    with ns.writer_lock(Path(project) / "book"):
        state = ns.load_state(Path(project) / "book")
        wf = load_workflow(project)
        ns.require(not state["chapters"] and not wf["selected_topic"], "已选题或已有正文；更换目标平台需明确制定迁移方案。")
        wf["platform"] = platform; wf["research_path"] = None; wf["topics_path"] = None
        write_workflow(project, wf, "RESEARCH_PLATFORM", "已选择" + catalog(project)[platform]["name"] + "，正在等待核查现行规则。", "核查官方规则和公开榜单，保留来源与时间，再提出5—10个原创选题。")
    return build_index(project)

def set_task(project, phase, detail, next_action, chapter=None):
    ns.require(bool(detail.strip()) and bool(next_action.strip()), "任务应说明当前工作和下一步。")
    with ns.writer_lock(Path(project) / "book"):
        wf = load_workflow(project); state, _ = current_state(project)
        if phase in ("WRITING", "REVIEWING", "REVISING", "CHECKPOINT"):
            ns.require(wf["selected_topic"] is not None and set(state["inputs"]) == {"canon", "roadmap"}, "先选题并完成作者设定和故事路线。")
        if phase == "WAITING_TOPIC": ns.require(wf["topics_path"] is not None, "先保存5—10个选题。")
        if phase == "COMPLETE": ns.require(state["completion"] is not None, "先完成核心工具的完稿检查。")
        if phase == 'WAITING_NEXT': ns.require(bool(state['chapters']), '先完成至少一章。')
        if chapter is not None:
            ns.require(1 <= chapter <= state["config"]["target_chapters"], "当前章号无效。")
            wf["chapter"] = chapter
        write_workflow(project, wf, phase, detail, next_action)
    return build_index(project)

def official_url(url, domains):
    parsed = urlparse(url)
    return parsed.scheme == "https" and not parsed.username and not parsed.password and any(parsed.hostname == d or (parsed.hostname or "").endswith("." + d) for d in domains)

def save_research(project, path):
    report = ns.read_json(path)
    with ns.writer_lock(Path(project) / "book"):
        wf = load_workflow(project); platforms = catalog(project)
        ns.require(wf["platform"] and report.get("platform") == wf["platform"], "研究报告与当前平台不符。")
        ns.require(report.get("coverage_status") in ("verified", "partial"), "必须如实声明覆盖程度。")
        ns.require(bool(report.get("checked_at")) and isinstance(report.get("sources"), list) and report["sources"], "缺少核查时间或官方来源。")
        for item in report["sources"]:
            ns.require(item.get("id") and item.get("title") and item.get("accessed_at") and official_url(item.get("url", ""), platforms[wf["platform"]]["official_domains"]), "研究来源须有编号、标题、时间与平台官方链接。")
        ns.require(isinstance(report.get("rules"), list) and isinstance(report.get("rank_samples"), list), "缺少规则和榜单样本记录。")
        ns.require(isinstance(report.get("gaps"), list), "未核实项必须明确列出，可为空列表。")
        ids = [x["id"] for x in report["sources"]]
        ns.require(len(ids) == len(set(ids)), "来源编号不能重复。")
        for rule in report["rules"]:
            ns.require(rule.get("status") in ("verified", "unknown") and bool(rule.get("topic")), "规则需区分已核实与未核实。")
            if rule["status"] == "verified": ns.require(rule.get("source_id") in ids, "已核实规则缺少可查来源。")
        for sample in report["rank_samples"]:
            ns.require(all(sample.get(k) for k in ("title", "author", "list_name", "observed_at", "source_id")), "榜单样本缺少标题、作者、榜单或时间。")
            ns.require(sample["source_id"] in ids and official_url(sample.get("book_url", ""), platforms[wf["platform"]]["official_domains"]), "榜单样本缺少官方来源。")
        ns.require(report["coverage_status"] != "verified" or len(report["rank_samples"]) >= 5, "完整扫榜至少需要5个有来源样本，否则标记partial。")
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        relative = "research/platform_" + ns.digest(encoded) + ".json"
        ns.immutable_text(ns.inside(project, relative), encoded)
        wf["research_path"] = relative
        write_workflow(project, wf, "SCANNING_RANKINGS", "规则和榜单资料已保存；" + ("存在待核实项。" if report["coverage_status"] == "partial" else "已记录可回查来源。"), "根据研究形成5—10个原创候选，说明长篇动力和风险。")
    return build_index(project)

def save_topics(project, path):
    data = ns.read_json(path); items = data.get("candidates", [])
    ns.require(isinstance(items, list) and 5 <= len(items) <= 10, "请提供5—10个原创选题。")
    ns.require(len({x.get("id") for x in items}) == len(items), "选题编号不可重复。")
    for item in items:
        ns.require(all(isinstance(item.get(k), str) and item[k].strip() for k in ("id", "title", "genre", "pitch", "reader_promise", "long_form_engine", "stage_variation", "risk")), "选题需有标题、题材、卖点、读者承诺、长篇动力、阶段变化和风险。")
    with ns.writer_lock(Path(project) / "book"):
        wf = load_workflow(project)
        ns.require(wf["platform"] and wf["research_path"] and wf["selected_topic"] is None, "先保存平台研究，已有选定题材不得静默替换。")
        data["platform"] = wf["platform"]
        encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        relative = "research/topics_" + ns.digest(encoded) + ".json"
        ns.immutable_text(ns.inside(project, relative), encoded)
        wf["topics_path"] = relative
        write_workflow(project, wf, "WAITING_TOPIC", f"已准备{len(items)}个原创选题，等待作者选择。", "作者选择候选编号后，进行长篇规划；当前不写正文。")
    return build_index(project)

def select_topic(project, topic_id, request):
    ns.require(bool(request.strip()), "记录作者实际选择。")
    with ns.writer_lock(Path(project) / "book"):
        wf = load_workflow(project); state = ns.load_state(Path(project) / "book")
        ns.require(wf["phase"] == "WAITING_TOPIC" and not state["chapters"], "当前不是等待选题状态。")
        candidates = ns.read_json(ns.inside(project, wf["topics_path"]))["candidates"]
        chosen = next((x for x in candidates if x["id"] == topic_id), None)
        ns.require(chosen is not None, "选题编号不存在。")
        wf["selected_topic"] = dict(chosen, author_request=request.strip())
        state["title"] = chosen["title"]; state["revision"] += 1
        ns.atomic_json(Path(project) / "book/state.json", state)
        write_workflow(project, wf, "PLANNING", "已选定《" + chosen["title"] + "》，准备长篇规划。", "建立作者设定、阶段因果、结局条件和近期3—5章路线。")
    return build_index(project)

def build_index(project, save=True):
    project = Path(project).resolve(); root = project / "book"
    state, sealed = current_state(project)
    workflow = load_workflow(project); cfg = get_config(project)
    ns.require(workflow["book_id"] == state["book_id"], "工作流与小说身份不符。")
    phase = workflow["phase"]; research = None; topics = []
    if workflow["research_path"]: research = ns.read_json(ns.inside(project, workflow["research_path"]))
    if workflow["topics_path"]: topics = ns.read_json(ns.inside(project, workflow["topics_path"]))["candidates"]
    data = {"schema_version": 1, "system_version": "4.2-serial", "initialized": True,
            "book": {"id": state["book_id"], "title": state["title"], "platform": workflow["platform"], "selected_topic_id": workflow["selected_topic"]["id"] if workflow["selected_topic"] else None},
            "source": {"repository_url": cfg["authority"]["repository_url"], "branch": cfg["authority"]["branch"], "generated_at": now(), "state_revision": state["revision"], "workflow_revision": workflow["revision"], "state_sha256": state['state_sha256'] if sealed else ns.digest((root / "state.json").read_text(encoding="utf-8")), "workflow_sha256": ns.digest((root / "workflow.json").read_text(encoding="utf-8"))},
            "progress": {"target_chapters": state["config"]["target_chapters"], "adopted_chapters": len(state["chapters"]), "target_han": state["config"]["target_han"], "actual_han": sum(x["han_count"] for x in state["chapters"].values()), "completed": state["completion"] is not None, "needs_revalidation": state["needs_revalidation"], **({k:state['progress'][k] for k in ('reviewed_until','window_review_due','unresolved_reviews')} if sealed else ns.review_status(state))},
            "task": {"phase": phase, "label": PHASES[phase], "chapter": workflow["chapter"], "detail": workflow.get("detail", ""), "next_action": workflow.get("next_action", ""), "updated_at": workflow["updated_at"]},
            "chapters": [{"number": int(n), "title": item["title"], "stage": item["stage"], "han_count": item["han_count"], "text_path": "book/" + item["text_path"], "sha256": item["text_sha256"], "needs_revalidation": int(n) in state["needs_revalidation"]} for n, item in sorted(state["chapters"].items(), key=lambda x:int(x[0]))],
            "topics": topics, "research": research, "history": workflow.get("history", [])[-15:]}
    if sealed:
        pointer, envelope = sealed
        data['source']['manuscript_storage'] = 'encrypted_book_v1'
        data['source']['vault_sha256'] = pointer['sha256']
        for chapter in data['chapters']:
            chapter.update(storage='encrypted_book_v1', vault_path=pointer['path'], vault_sha256=pointer['sha256'],
                           book_id=state['book_id'], key_id=pointer['key_id'])
        ns.require(ns.read_json(root / 'private_storage.json') == pointer, '索引生成期间私密稿改变，请重试。')
    else:
        data['source']['manuscript_storage'] = 'plain_files_v1'
        data['source']['requires_reader_key'] = False
        if state.get('serialization', {}).get('enabled'):
            data['progress'].update(ns.serial_module().summary(state))
            for chapter in data['chapters']:
                receipt = state['serialization']['published'].get(str(chapter['number']))
                chapter.update(frozen=True, publication_status='confirmed' if receipt else 'unreported',
                               published_at=receipt['published_at'] if receipt else None)
        ns.require(ns.load_state(root)['revision'] == state['revision'], '索引生成期间正式状态改变，请重试。')
    ns.require(load_workflow(project)["revision"] == workflow["revision"], "索引生成期间正式状态改变，请重试。")
    if save: ns.atomic_json(project / "online/index.json", data)
    return data

def validate_index(project):
    expected = build_index(project, save=False); actual = ns.read_json(Path(project) / "online/index.json")
    expected["source"].pop("generated_at", None); actual["source"].pop("generated_at", None)
    ns.require(expected == actual, "阅读索引落后于正式状态，请先build-index再提交。")
    return {"valid": True, "chapters": len(actual["chapters"]), "phase": actual["task"]["phase"]}

def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--project", default=".")
    subs = parser.add_subparsers(dest="command", required=True)
    p=subs.add_parser("bootstrap"); p.add_argument("--repo", required=True); p.add_argument("--branch", default="main"); p.add_argument("--title", default="未命名小说")
    p=subs.add_parser("select-platform"); p.add_argument("--platform", required=True)
    p=subs.add_parser("task"); p.add_argument("--phase", choices=list(PHASES), required=True); p.add_argument("--detail", required=True); p.add_argument("--next", required=True); p.add_argument("--chapter", type=int)
    for name in ("research", "topics"):
        p=subs.add_parser(name); p.add_argument("--file", required=True)
    p=subs.add_parser("select-topic"); p.add_argument("--id", required=True); p.add_argument("--request", required=True)
    subs.add_parser("build-index"); subs.add_parser("validate-index")
    args=parser.parse_args()
    try:
        if args.command=="bootstrap": result=bootstrap(args.project,args.repo,args.branch,args.title)
        elif args.command=="select-platform": result=select_platform(args.project,args.platform)
        elif args.command=="task": result=set_task(args.project,args.phase,args.detail,args.next,args.chapter)
        elif args.command=="research": result=save_research(args.project,args.file)
        elif args.command=="topics": result=save_topics(args.project,args.file)
        elif args.command=="select-topic": result=select_topic(args.project,args.id,args.request)
        elif args.command=="build-index": result=build_index(args.project)
        else: result=validate_index(args.project)
        print(json.dumps(result if args.command in ("bootstrap","validate-index") else {"phase": result["task"]["phase"], "updated_at": result["source"]["generated_at"], "chapters": len(result["chapters"])},ensure_ascii=False,indent=2))
    except (ns.WorkspaceError,OSError,ValueError,KeyError,TypeError) as exc: parser.exit(2,f"未完成：{exc}\n")

if __name__ == "__main__": main()
