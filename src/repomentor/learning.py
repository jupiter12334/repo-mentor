"""反馈学习工作流：提炼、关联、幂等保存候选；人工确认才会生效。"""

import fcntl
import uuid

from repomentor.store import Store
from repomentor.workflow import WorkflowJournal, validate_extraction

EXTRACT = (
    "把评审反馈整理成一条可复用规范，不能扩大范围、虚构例外。"
    "只适用于本次任务的要求必须 persistent=false。"
    "缺少明确可执行要求的讨论、解释、确认或感谢也返回 persistent=false。"
    '输出 JSON：{"persistent":true,"requirement":"要求","condition":"适用条件","exceptions":"例外"}。'
)
RELATE = (
    "比较新反馈与每一条现有规范，逐条输出关系；只是建议，不修改生效规范。"
    "equivalent=同义重复，refinement=兼容细化，conflict=要求互斥，independent=无关，uncertain=无法判断。"
    '输出 JSON：{"relations":[{"id":"R...","version":1,"relation":"conflict","reason":"原因"}]}。'
)


def relate_rules(journal: WorkflowJournal, data: dict, paths: list[str], rules: list) -> dict:
    """反馈提炼和对话草稿共用规范关联校验，不让模型直接改动生效规范。"""
    if not rules:
        return {"relations": []}

    def validate(result):
        relations = result["relations"]
        expected = {(r.id, r.version) for r in rules}
        if not isinstance(relations, list) or len(relations) != len(expected):
            raise ValueError("关联结果缺少规范")
        if {(r["id"], r["version"]) for r in relations} != expected:
            raise ValueError("关联规范或版本不匹配")
        if any(
            r["relation"]
            not in {"equivalent", "refinement", "conflict", "independent", "uncertain"}
            or not isinstance(r.get("reason"), str)
            for r in relations
        ):
            raise ValueError("非法的关联判定")

    return journal.step(
        "relate",
        RELATE,
        {"new_rule": data, "scope": paths, "existing_rules": [r.to_dict() for r in rules]},
        validate,
    )


def new_learning(
    store: Store,
    repo_id: str,
    text: str,
    paths: list[str],
    *,
    workflow_id: str | None = None,
    **options,
) -> dict:
    if not text.strip() or not paths:
        raise ValueError("反馈正文和作用域不能为空")
    record = {
        "id": workflow_id or "learn-" + uuid.uuid4().hex[:10],
        "repo_id": repo_id,
        "text": text,
        "paths": paths,
        "options": options,
        "status": "pending",
    }
    return store.put_once("learning", record["id"], record)


def continue_learning(store: Store, id: str, model) -> dict:
    lock_path = store.directory / f"{id}.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("该反馈工作流正在被其他进程推进") from None
        record = store.get("learning", id)
        if record["status"] == "completed":
            return record
        if record["options"].get("provenance"):
            from repomentor.reviews import validate_provenance

            validate_provenance(store, record["options"]["provenance"])
        journal = WorkflowJournal(store, id, model)
        record.update(status="running", error=None)
        store.put("learning", id, record)
        try:
            data = journal.step(
                "extract",
                EXTRACT,
                {"feedback": record["text"], "scope": record["paths"]},
                validate_extraction,
            )
            record["extraction"] = data
            if not data["persistent"]:
                record.update(
                    status="completed",
                    stored=False,
                    reason="反馈未形成可复用的长期要求，不进入规范记忆",
                )
            else:
                rules = store.rules(record["repo_id"])

                related = relate_rules(journal, data, record["paths"], rules)
                record["relations"] = related["relations"]
                options = dict(record["options"])
                if options.get("kind") == "exception":
                    # 文件例外的豁免范围来自显式参数，模型不能扩展成条件型豁免。
                    data = {**data, "condition": ""}
                    related = {"relations": []}
                candidate = store.propose(
                    record["repo_id"],
                    data["requirement"],
                    paths=record["paths"],
                    condition=data["condition"],
                    exceptions=data["exceptions"],
                    source=(
                        "代码评审：" + options["provenance"]["comment_url"]
                        if options.get("provenance")
                        else "开发者反馈：" + record["text"]
                    ),
                    learning_id=id,
                    relations=related["relations"],
                    **options,
                )
                record.update(candidate=candidate.to_dict(), status="completed", stored=True)
        except BaseException as error:
            record.update(
                status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                error=type(error).__name__,
            )
            store.put("learning", id, record)
            raise
        store.put("learning", id, record)
        return record
