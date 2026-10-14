"""对可精确描述的规范执行 AST 检查，避免让模型猜测执行结果。"""

import ast
import fnmatch
from pathlib import Path

from repomentor.models import Rule
from repomentor.reviewer import exception_paths


def check_rules(root: Path, rules: list[Rule]) -> tuple[list[dict], list[str]]:
    findings, unchecked = [], []
    for rule in rules:
        if rule.kind == "exception":
            continue
        checker = rule.checker
        if not checker:
            unchecked.append(rule.id)
            continue
        kind = checker.get("type")
        waived = exception_paths(rule, rules)
        if kind == "query_bounds":
            for name in rule.paths:
                if (
                    not any(c in name for c in "*?[")
                    and name not in waived
                    and not (root / name).is_file()
                ):
                    findings.append(
                        {
                            "rule_id": rule.id,
                            "file": name,
                            "line": 1,
                            "message": "规范指定的文件被删除",
                        }
                    )
        if kind not in {"forbid_import", "query_bounds"}:
            raise ValueError(f"不支持的规范检查器：{kind}")
        for path in sorted(root.rglob("*.py")):
            name = path.relative_to(root).as_posix()
            if not any(fnmatch.fnmatch(name, pattern) for pattern in rule.paths):
                continue
            if name in waived:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=name)
            if kind == "forbid_import":
                forbidden = checker["modules"]
                for node in ast.walk(tree):
                    imports = (
                        [a.name for a in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                        if isinstance(node, ast.ImportFrom)
                        else []
                    )
                    if any(n == m or n.startswith(m + ".") for n in imports for m in forbidden):
                        findings.append(
                            {
                                "rule_id": rule.id,
                                "file": name,
                                "line": node.lineno,
                                "message": rule.requirement,
                            }
                        )
            else:
                findings.extend(_query_bounds(tree, name, rule))
    return findings, unchecked


def _query_bounds(tree, name: str, rule: Rule) -> list[dict]:
    checker = rule.checker
    findings = []
    seen = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if checker.get("functions") and function.name not in checker["functions"]:
            continue
        seen.add(function.name)
        arguments = [*function.args.posonlyargs, *function.args.args]
        defaults = [None] * (len(arguments) - len(function.args.defaults)) + function.args.defaults
        parameters = set(checker.get("parameters", ["limit"]))
        if checker.get("functions"):
            missing = parameters - {a.arg for a in arguments}
            if missing:
                findings.append(
                    {
                        "rule_id": rule.id,
                        "file": name,
                        "line": function.lineno,
                        "message": "规范指定的参数被删除：" + ", ".join(sorted(missing)),
                    }
                )
        for arg, default in zip(arguments, defaults):
            if arg.arg not in checker.get("parameters", ["limit"]):
                continue
            query = next(
                (
                    n
                    for container in (arg.annotation, default)
                    if container is not None
                    for n in ast.walk(container)
                    if isinstance(n, ast.Call)
                    and (
                        (isinstance(n.func, ast.Name) and n.func.id == "Query")
                        or (isinstance(n.func, ast.Attribute) and n.func.attr == "Query")
                    )
                ),
                None,
            )
            keywords = (
                {k.arg: k.value.value for k in query.keywords if isinstance(k.value, ast.Constant)}
                if query
                else {}
            )
            if any(keywords.get(k) != checker[k] for k in ("ge", "le") if k in checker):
                findings.append(
                    {
                        "rule_id": rule.id,
                        "file": name,
                        "line": arg.lineno,
                        "message": rule.requirement,
                    }
                )
    for missing in set(checker.get("functions", [])) - seen:
        findings.append(
            {
                "rule_id": rule.id,
                "file": name,
                "line": 1,
                "message": "规范指定的函数被删除或改名：" + missing,
            }
        )
    return findings
