"""仓库身份、任务工作副本和内容指纹。"""

import hashlib
import os
import subprocess
from pathlib import Path


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(f"Git 操作失败：{result.stderr.strip()[:400]}")
    return result.stdout.strip()


def git_names(root: Path, *args: str) -> list[str]:
    # 文件名不能用换行或 Git 的展示转义解析，中文、空格也必须参与规范验收。
    result = subprocess.run(
        ["git", "-C", str(root), args[0], "-z", *args[1:]], capture_output=True, check=True
    )
    return [name.decode("utf-8") for name in result.stdout.split(b"\0") if name]


def repo_identity(root: Path) -> str:
    root = root.resolve()
    if not is_git(root):
        return hashlib.sha256(("directory:" + str(root)).encode()).hexdigest()[:20]
    common = Path(git(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    # 同一仓库的各个 worktree 共用身份，其他本地仓库不会意外共享记忆。
    top = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    subdirectory = root.relative_to(top).as_posix()
    identity = str(common.resolve()) + (":" + subdirectory if subdirectory != "." else "")
    return hashlib.sha256(identity.encode()).hexdigest()[:20]


def is_git(root: Path) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
        ).returncode
        == 0
    )


def has_head(root: Path) -> bool:
    return (
        is_git(root)
        and subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], capture_output=True
        ).returncode
        == 0
    )


def protected_parts(parts) -> bool:
    return any(
        p in {".git", ".env", ".repomentor", ".venv"} or p.startswith(".env.") for p in parts
    )


def safe_path(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("路径越过仓库边界")
    relative = path.relative_to(root.resolve())
    if protected_parts(relative.parts):
        raise ValueError("该路径不允许模型访问")
    return path


def tracked_files(root: Path) -> list[Path]:
    if is_git(root):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                ".",
            ],
            check=True,
            capture_output=True,
        )
        names = sorted(set(n.decode() for n in result.stdout.split(b"\0") if n))
    else:
        names = []
        for directory, folders, files in os.walk(root, followlinks=False):
            folders[:] = [
                f
                for f in folders
                if not protected_parts([f])
                and f
                not in {
                    "__pycache__",
                    "node_modules",
                    ".pytest_cache",
                    ".ruff_cache",
                    "dist",
                    "build",
                }
            ]
            names.extend((Path(directory) / f).relative_to(root).as_posix() for f in files)
        names.sort()
    paths = []
    for name in names:
        try:
            path = safe_path(root, name)
        except ValueError:
            continue
        if path.is_file():
            # 不把仓库外的符号链接内容带入验证快照。
            if (root / name).is_symlink():
                raise ValueError(f"验证暂不支持符号链接：{name}")
            paths.append(path)
    return paths


def fingerprint(root: Path) -> str:
    value = hashlib.sha256()
    for path in tracked_files(root):
        value.update(str(path.relative_to(root)).encode() + b"\0")
        value.update(path.read_bytes())
        value.update(b"\0")
    return value.hexdigest()


def create_worktree(repo: Path, state: Path, run_id: str) -> Path:
    if git(repo, "status", "--porcelain"):
        raise ValueError("源仓库有未提交修改；请提交后创建任务副本，或使用 --in-place")
    path = state / "worktrees" / run_id
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", f"codex/repomentor/{run_id}", str(path), "HEAD")
    return path
