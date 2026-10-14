"""按仓库、版本和完整目录路径保存架构；确定性检索并检查明确的路径边界。"""

import ast
import json
import re
from copy import deepcopy
from pathlib import Path, PurePosixPath

from jsonschema import ValidationError, validate

from repomentor.models import Rule
from repomentor.selection import digest
from repomentor.store import Store

ARCHITECTURE_VERSION = 1
TEXT = {"type": "string", "minLength": 1, "maxLength": 1500}
STRINGS = {"type": "array", "items": TEXT, "maxItems": 30, "uniqueItems": True}
NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": TEXT,
        "purpose": TEXT,
        "should_contain": STRINGS,
        "must_not_contain": STRINGS,
        "keywords": STRINGS,
        "related": STRINGS,
        "allow_files": {"type": "boolean"},
        "allow_subdirectories": {"type": "boolean"},
        "python_module": {"type": "string", "maxLength": 200},
        "forbidden_dependencies": STRINGS,
    },
    "required": ["path", "purpose"],
    "additionalProperties": False,
}
ARCHITECTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "directories": {"type": "array", "items": NODE_SCHEMA, "minItems": 1, "maxItems": 120},
        "code_extensions": STRINGS,
        "python_roots": STRINGS,
    },
    "required": ["summary", "directories"],
    "additionalProperties": False,
}


def directory_path(value: str) -> str:
    if value == ".":
        return value
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(
            p in {"", ".", "..", ".git", ".env", ".repomentor", ".venv"} or p.startswith(".env.")
            for p in value.split("/")
        )
        or any(c in value for c in "*?[\n\r\0")
    ):
        raise ValueError("架构目录必须是规范的仓库内完整相对路径，不允许通配符")
    return value


def contains(directory: str, path: str) -> bool:
    return directory == "." or path == directory or path.startswith(directory + "/")


def normalize_spec(spec: dict) -> dict:
    try:
        validate(spec, ARCHITECTURE_SCHEMA)
    except ValidationError:
        raise ValueError("架构格式无效，请按 summary、directories 和工具 schema 填写") from None
    spec = deepcopy(spec)
    spec["summary"] = spec["summary"].strip()
    if not spec["summary"]:
        raise ValueError("架构总览不能为空")
    nodes = spec["directories"]
    paths = [directory_path(n["path"]) for n in nodes]
    if len(set(paths)) != len(paths) or "." not in paths:
        raise ValueError("目录路径不能重复，且必须有根目录 . 的总览说明")
    for node in nodes:
        node["purpose"] = node["purpose"].strip()
        if not node["purpose"]:
            raise ValueError("目录职责不能为空")
        for field in (
            "should_contain",
            "must_not_contain",
            "keywords",
            "related",
            "forbidden_dependencies",
        ):
            node.setdefault(field, [])
            if any(not value.strip() for value in node[field]):
                raise ValueError("架构说明条目不能为空白文本")
        node.setdefault("allow_files", node["path"] != ".")
        node.setdefault("allow_subdirectories", True)
        node.setdefault("python_module", "")
        if node["python_module"] and not re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", node["python_module"]
        ):
            raise ValueError("python_module 必须是有效的 Python 模块前缀")
        for target in node["related"] + node["forbidden_dependencies"]:
            if target not in paths or target == node["path"]:
                raise ValueError("相关目录和禁止依赖必须引用其他已登记的完整路径")
    by_path = {n["path"]: n for n in nodes}
    for node in nodes:
        if any(not by_path[p]["python_module"] for p in node["forbidden_dependencies"]):
            raise ValueError("禁止依赖的目标目录需要声明 python_module")
    spec.setdefault("code_extensions", [".py", ".js", ".jsx", ".ts", ".tsx", ".vue"])
    if not spec["code_extensions"] or any(
        not re.fullmatch(r"\.[A-Za-z0-9]+", e) for e in spec["code_extensions"]
    ):
        raise ValueError("code_extensions 必须是非空扩展名列表，例如 .py")
    spec.setdefault("python_roots", ["."])
    if not spec["python_roots"]:
        raise ValueError("python_roots 不能为空")
    for path in spec["python_roots"]:
        directory_path(path)
    spec["directories"] = sorted(nodes, key=lambda n: n["path"])
    if len(json.dumps(spec, ensure_ascii=False)) > 60000:
        raise ValueError("架构说明超过 60000 字符，请精简职责描述")
    return spec


class ArchitectureMemory:
    """目录说明逐条存储，确认整个版本时原子发布，避免半新半旧的架构。"""

    def __init__(self, store: Store):
        self.store = store

    def get(self, repo_id: str, version: int | None = None) -> dict | None:
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM architectures WHERE repo_id=? AND "
                + ("status='active'" if version is None else "version=?"),
                (repo_id,) if version is None else (repo_id, version),
            ).fetchone()
            if row is None:
                return None
            record = {**json.loads(row["data"]), "status": row["status"]}
            record["spec"]["directories"] = [
                json.loads(n["data"])
                for n in db.execute(
                    "SELECT data FROM architecture_nodes WHERE repo_id=? AND version=? ORDER BY path",
                    (repo_id, row["version"]),
                )
            ]
            return record

    def propose(self, repo_id: str, spec: dict, source: str = "人工录入") -> dict:
        spec = normalize_spec(spec)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM architectures WHERE repo_id=?", (repo_id,)
            ).fetchone()[0]
            active = db.execute(
                "SELECT version FROM architectures WHERE repo_id=? AND status='active'", (repo_id,)
            ).fetchone()
            record = {
                "id": "Architecture",
                "repo_id": repo_id,
                "version": version,
                "status": "candidate",
                "base_version": active[0] if active else None,
                "spec": {k: v for k, v in spec.items() if k != "directories"},
                "source": source,
            }
            db.execute(
                "INSERT INTO architectures VALUES(?,?,?,?)",
                (repo_id, version, "candidate", json.dumps(record, ensure_ascii=False)),
            )
            db.executemany(
                "INSERT INTO architecture_nodes VALUES(?,?,?,?)",
                [
                    (repo_id, version, n["path"], json.dumps(n, ensure_ascii=False))
                    for n in spec["directories"]
                ],
            )
        return self.get(repo_id, version)

    def confirm(self, repo_id: str, version: int, expected_hash: str | None = None) -> dict:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT version,status FROM architectures WHERE repo_id=? ORDER BY version DESC LIMIT 1",
                (repo_id,),
            ).fetchone()
            if row is None or row["version"] != version or row["status"] != "candidate":
                raise ValueError("架构候选不存在或已变化，请重新展示并确认最新版本")
            # 当前事务持有写锁，读取完整候选后不会被其他进程插入新版本。
            candidate = self.get(repo_id, version)
            if expected_hash is not None and digest(candidate) != expected_hash:
                raise ValueError("架构候选内容已变化，请重新确认")
            active = db.execute(
                "SELECT version FROM architectures WHERE repo_id=? AND status='active'", (repo_id,)
            ).fetchone()
            if (active[0] if active else None) != candidate["base_version"]:
                raise ValueError("生效架构已变化，请重新起草")
            db.execute("UPDATE architectures SET status='superseded' WHERE repo_id=?", (repo_id,))
            db.execute(
                "UPDATE architectures SET status='active' WHERE repo_id=? AND version=?",
                (repo_id, version),
            )
        return self.get(repo_id)


def architecture_hash(record: dict | None) -> str:
    return digest(record)


def ancestors(spec: dict, path: str) -> list[dict]:
    return sorted(
        (n for n in spec["directories"] if contains(n["path"], path)),
        key=lambda n: len(PurePosixPath(n["path"]).parts),
    )


def overview(record: dict | None) -> dict:
    if not record:
        return {"status": "not_configured"}
    return {
        "format_version": ARCHITECTURE_VERSION,
        "version": record["version"],
        "summary": record["spec"]["summary"],
        "directory_index": [
            {"path": n["path"], "purpose": n["purpose"][:100]}
            for n in record["spec"]["directories"]
        ],
    }


def architecture_context(record: dict | None, paths: list[str], query: str = "") -> dict:
    if not record:
        return {"status": "not_configured"}
    spec = record["spec"]
    nodes = {n["path"]: n for n in spec["directories"]}
    selected = {"."}
    matches = {}
    for path in paths:
        directory_path(path)
        chain = ancestors(spec, path if path in nodes else str(PurePosixPath(path).parent))
        selected.update(n["path"] for n in chain)
        matches[path] = [n["path"] for n in chain]
    # 文本仅帮助编码前找到候选位置；验收覆盖始终由实际路径和父目录计算。
    hits = sorted(
        (n for n in nodes.values() if any(k.lower() in query.lower() for k in n["keywords"])),
        key=lambda n: n["path"],
    )[:6]
    selected.update(n["path"] for n in hits)
    processed = set()
    while selected - processed:
        path = sorted(selected - processed)[0]
        processed.add(path)
        selected.update(n["path"] for n in ancestors(spec, path))
        selected.update(nodes[path]["related"] + nodes[path]["forbidden_dependencies"])
    alternatives = set()
    for path in selected - {"."}:
        parent = str(PurePosixPath(path).parent)
        alternatives.update(p for p in nodes if str(PurePosixPath(p).parent) == parent)
    result = {
        "format_version": ARCHITECTURE_VERSION,
        "version": record["version"],
        "summary": spec["summary"],
        "placement_policy": "职责说明继承父目录；allow_files 和 allow_subdirectories 仅由最近的已登记目录决定，已登记子目录可开放代码存放。",
        "path_ancestors": matches,
        "directories": [nodes[p] for p in sorted(selected)],
        "alternatives": [
            {
                "path": nodes[p]["path"],
                "purpose": nodes[p]["purpose"],
                "should_contain": nodes[p]["should_contain"],
                "must_not_contain": nodes[p]["must_not_contain"],
            }
            for p in sorted(alternatives - selected)
        ],
    }
    if len(json.dumps(result, ensure_ascii=False)) > 22000:
        raise ValueError("相关架构上下文超过预算，请拆分任务或精简目录说明")
    return result


def is_code(spec: dict, path: str) -> bool:
    return PurePosixPath(path).suffix in spec["code_extensions"]


def placement_error(record: dict | None, path: str) -> str | None:
    if not record or not is_code(record["spec"], path):
        return None
    directory_path(path)
    parent = str(PurePosixPath(path).parent)
    chain = ancestors(record["spec"], parent)
    owner = chain[-1]
    if not owner["allow_files"]:
        return f"目录 {owner['path']} 不允许直接或在未登记子目录内放置代码文件"
    if not owner["allow_subdirectories"] and owner["path"] != parent:
        return f"目录 {owner['path']} 不允许在未登记子目录内放置代码文件"
    return None


def architecture_rule(record: dict | None, paths: list[str]) -> Rule | None:
    if not record:
        return None
    targets = sorted(p for p in set(paths) if is_code(record["spec"], p))
    if not targets:
        return None
    return Rule(
        "Architecture",
        record["repo_id"],
        record["version"],
        "新增和修改的代码必须符合 architecture 中其完整路径对应的目录职责及所有父目录约束；"
        "不得把另一层的职责放进当前目录。结合 alternatives 判断放错层的代码。"
        "路径符合并不等于职责符合；上下文不足须判 uncertain。",
        paths=targets,
        status="active",
        mandatory=True,
        source=record["source"],
    )


def imported_modules(tree, path: str, roots: list[str]) -> list[tuple[int, str]]:
    candidates = [r for r in roots if contains(r, path)]
    root = max(candidates, key=len) if candidates else "."
    relative = PurePosixPath(path) if root == "." else PurePosixPath(path).relative_to(root)
    package = list(relative.parent.parts)
    result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > len(package):
                    raise ValueError(f"相对导入越过已登记 Python 根目录：{path}:{node.lineno}")
                base = package[: len(package) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            result.append((node.lineno, module))
            result.extend((node.lineno, module + "." + alias.name) for alias in node.names)
    return result


def check_architecture(
    snapshot: Path, record: dict | None, paths: list[str], created_files: list[str]
) -> list[dict]:
    if not record:
        return []
    findings = []
    for path in created_files:
        error = placement_error(record, path)
        if error:
            findings.append({"rule_id": "Architecture", "file": path, "line": 1, "message": error})
    nodes = {n["path"]: n for n in record["spec"]["directories"]}
    for name in sorted(set(paths)):
        path = snapshot / name
        if path.suffix != ".py" or not path.is_file():
            continue
        forbidden = {
            p
            for n in ancestors(record["spec"], str(PurePosixPath(name).parent))
            for p in n["forbidden_dependencies"]
        }
        if not forbidden:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        for line, module in imported_modules(tree, name, record["spec"]["python_roots"]):
            for target in sorted(forbidden):
                prefix = nodes[target]["python_module"]
                if module == prefix or module.startswith(prefix + "."):
                    finding = {
                        "rule_id": "Architecture",
                        "file": name,
                        "line": line,
                        "message": f"架构禁止依赖 {target}（导入 {module}）",
                    }
                    if finding not in findings:
                        findings.append(finding)
    return findings
