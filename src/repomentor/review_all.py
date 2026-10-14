"""全部变更的 Jev 优先评审：有限判断通过后，LLM 只处理需要复核的项目。"""

import fnmatch

from repomentor.judgement import evidence_candidates
from repomentor.reviewer import INSTRUCTIONS
from repomentor.workflow import WorkflowJournal

ALL_INSTRUCTIONS = INSTRUCTIONS + (
    "本次还要逐条评审 changes 字段中的文件改动，包括功能错误和架构目录职责。"
    "changes 结果必须且只能覆盖 expected_change_paths，每项包含 path、verdict、reason、findings。"
    "changes 的 verdict 只允许 pass、violation、uncertain，证据要求与规范相同。"
    "相关未改文件只是依赖资料，不单独评审，不因历史代码的问题判本次改动违规。"
    "通用改动检查也尊重已批准规范例外，不借改动检查撤销例外或发明团队风格规范。"
    "confirmed_rules 是已确认规范的参考资料；只评审 expected_rule_ids，不额外增加条目。"
    "删除文件的旧正文在 deleted_files；diff 提供增删上下文。"
    "check_task=true 才评审 task；check_task=false 时返回 task=null，不重复任务目标评审。"
    "不需要复核的规则和改动已经由宿主处理，不能额外增加 results 或 changes。"
    '输出 JSON：{"results":[],"changes":[{"path":"完整相对路径",'
    '"verdict":"pass|violation|uncertain","reason":"理由","findings":[]}],"task":null}。'
)


def review_all_changes(reviewer, review_id: str, payload: dict, version: int) -> dict:
    store, model, judge = reviewer.store, reviewer.model, reviewer.judge
    rule_by_id = {r["id"]: r for r in payload["rules"]}
    change_by_path = {c["path"]: c for c in payload["changes"]}
    contracts = {
        **{"rule::" + id: ("rule", id) for id in rule_by_id},
        **{"change::" + path: ("change", path) for path in change_by_path},
        "task::goal": ("task", None),
    }
    judgement = judge.judge(payload) if judge else None
    decisions = (judgement or {}).get("decisions", {})
    candidates = evidence_candidates(payload)
    files = {**payload.get("deleted_files", {}), **payload["files"]}
    accepted = {}
    for question_id, (kind, name) in contracts.items():
        decision = decisions.get(question_id)
        if decision and decision["confidence"] >= judge.min_confidence:
            verdict = decision["verdict"]
            findings = []
            if verdict == "violation":
                pointer = decisions.get("evidence::" + question_id)
                if (
                    not pointer
                    or pointer["confidence"] < judge.min_confidence
                    or pointer["verdict"] not in candidates[question_id]
                ):
                    continue
                finding = candidates[question_id][pointer["verdict"]]
                findings = [
                    {**finding, "message": "Jev 判断此源码行支持当前规范、改动或任务目标违规。"}
                ]
            elif verdict == "not_applicable":
                if kind != "rule":
                    continue
                rule = rule_by_id[name]
                matched = {
                    p
                    for p in files
                    if any(fnmatch.fnmatch(p, pattern) for pattern in rule["paths"])
                }
                if not matched or not matched <= set(rule["waived_paths"]):
                    continue
            elif verdict not in {"pass", "uncertain"}:
                continue
            item = {
                "verdict": verdict,
                "reason": "Jev 根据固定快照、完整路径及相关架构作高置信度有限判断；不确定时需要补充相关实现。",
                "findings": findings,
                "backend": "jev",
                "confidence": decision["confidence"],
            }
            if kind == "rule":
                item["rule_id"] = name
            elif kind == "change":
                item["path"] = name
            reviewer._validate_item(
                item, files, {"pass", "violation", "uncertain", "not_applicable"}
            )
            accepted[question_id] = item
    pending = {id: contract for id, contract in contracts.items() if id not in accepted}
    llm_rule_ids = [name for kind, name in pending.values() if kind == "rule"]
    llm_paths = [name for kind, name in pending.values() if kind == "change"]
    check_task = "task::goal" in pending
    result = {"results": [], "changes": [], "task": None}

    def validate(data):
        if not isinstance(data, dict):
            raise ValueError("评审结果必须是对象")
        for field, key, expected in (
            ("results", "rule_id", llm_rule_ids),
            ("changes", "path", llm_paths),
        ):
            values = data.get(field)
            if (
                not isinstance(values, list)
                or len(values) != len(expected)
                or {i.get(key) for i in values} != set(expected)
            ):
                raise ValueError(f"{field} 必须且只能覆盖指定评审目标：{expected}")
            for item in values:
                verdicts = {"pass", "violation", "uncertain"}
                if field == "results":
                    verdicts.add("not_applicable")
                reviewer._validate_item(item, files, verdicts)
                if field == "results":
                    rule = rule_by_id[item[key]]
                    if any(
                        f["file"] in rule["waived_paths"]
                        or not any(fnmatch.fnmatch(f["file"], p) for p in rule["paths"])
                        for f in item["findings"]
                    ):
                        raise ValueError("评审问题超出规范范围或忽略已批准例外")
                elif any(f["file"] != item["path"] for f in item["findings"]):
                    raise ValueError("文件改动证据必须引用该完整路径")
        if check_task:
            reviewer._validate_item(data["task"], files, {"pass", "violation", "uncertain"})
        elif data.get("task") is not None:
            raise ValueError("无需重复评审任务，task 必须为 null")

    if pending:
        llm_payload = {
            **payload,
            "rules": [rule_by_id[id] for id in llm_rule_ids],
            "changes": [change_by_path[path] for path in llm_paths],
            "expected_rule_ids": llm_rule_ids,
            "expected_change_paths": llm_paths,
            "check_task": check_task,
            "confirmed_rules": payload["rules"],
        }
        result = WorkflowJournal(store, review_id, model).step(
            "review", ALL_INSTRUCTIONS, llm_payload, validate, version
        )

    combined = dict(accepted)
    llm_items = {"rule::" + i["rule_id"]: i for i in result["results"]}
    llm_items.update({"change::" + i["path"]: i for i in result["changes"]})
    if check_task:
        llm_items["task::goal"] = result["task"]
    for question_id, item in llm_items.items():
        item = {**item, "backend": "llm"}
        decision = decisions.get(question_id)
        if judge:
            item["routing_reason"] = (judgement or {}).get("fallback_reason") or (
                "Jev 违规需要源码证据，或结论不确定、不适用、置信度不足"
            )
        if decision:
            item["jev_decision"] = decision
            if (
                decision["verdict"] == "violation"
                and decision["confidence"] >= judge.min_confidence
                and item["verdict"] in {"pass", "not_applicable"}
            ):
                item.update(
                    llm_verdict=item["verdict"],
                    verdict="uncertain",
                    reason="Jev 可信判违规，但 LLM 判通过或不适用；需要复核。",
                )
        combined[question_id] = item
    rules = [combined["rule::" + id] for id in rule_by_id]
    changes = [combined["change::" + path] for path in change_by_path]
    task = combined["task::goal"]
    report = {
        "id": review_id,
        "model": model.name,
        "version": version,
        "scope": "all_changes",
        "passed": all(
            item["verdict"] in {"pass", "not_applicable"} for item in [*rules, *changes, task]
        ),
        "files": sorted(files),
        "results": rules,
        "changes": changes,
        "task": task,
        "rule_judgement": judgement,
        "llm_rule_ids": llm_rule_ids,
        "llm_change_paths": llm_paths,
        "llm_task": check_task,
        "jev_accepted_questions": sorted(accepted),
        "note": "模型语义评审不等于机器证明；高置信度也可能误判，测试和静态验收仍独立执行。",
    }
    store.put("review", review_id, report)
    return report
