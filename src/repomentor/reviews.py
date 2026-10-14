"""只读获取 PR/MR 评审，校验仓库和来源版本，再交给候选规范工作流。"""

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from repomentor.learning import continue_learning, new_learning
from repomentor.selection import digest
from repomentor.store import Store
from repomentor.workspace import git, safe_path

GITHUB_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
 repository(owner:$owner,name:$name) { pullRequest(number:$number) {
  url state headRefOid
  reviewThreads(first:50,after:$cursor) {
   pageInfo { hasNextPage endCursor }
   nodes { id isResolved isOutdated path line diffSide
    comments(first:50) { pageInfo { hasNextPage }
     nodes { databaseId body url updatedAt author { login __typename }
      pullRequestReview { state commit { oid } } }
    }
   }
  }
 } }
}
"""


def source_identity(url: str) -> dict:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port
    ):
        raise ValueError("评审地址必须是无认证信息的 HTTPS PR/MR 地址")
    path = parsed.path.rstrip("/")
    match = re.fullmatch(r"/([\w.-]+/[\w.-]+)/pull/(\d+)", path)
    if parsed.hostname == "github.com" and match:
        provider = "github"
    else:
        match = re.fullmatch(r"/([\w./-]+)/-/merge_requests/(\d+)", path)
        if not match:
            raise ValueError("不支持的 PR/MR 地址")
        provider = "gitlab"
    project, number = match.groups()
    if any(p in {"", ".", ".."} for p in project.split("/")):
        raise ValueError("评审项目路径无效")
    return {
        "provider": provider,
        "host": parsed.hostname.lower(),
        "project": project,
        "number": int(number),
        "url": f"https://{parsed.hostname}{path}",
    }


def assert_repo_source(repo: dict, source: dict):
    remote = git(Path(repo["path"]), "remote", "get-url", "origin")
    if remote.startswith("git@"):
        match = re.fullmatch(r"git@([^:]+):(.+)", remote)
        host, project = match.groups() if match else ("", "")
    else:
        parsed = urlparse(remote)
        host, project = parsed.hostname or "", parsed.path.lstrip("/")
    if project.endswith(".git"):
        project = project[:-4]
    if host.lower() != source["host"] or project.lower() != source["project"].lower():
        raise ValueError("评审来源与登记仓库的 origin 不匹配，不能跨仓库导入规范")


def _gh(source: dict, cursor=None) -> dict:
    owner, name = source["project"].split("/")
    argv = [
        "gh",
        "api",
        "graphql",
        "-f",
        "query=" + GITHUB_QUERY,
        "-f",
        "owner=" + owner,
        "-f",
        "name=" + name,
        "-F",
        f"number={source['number']}",
    ]
    if cursor:
        argv += ["-f", "cursor=" + cursor]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise ValueError("GitHub 评审读取失败，请检查 gh 登录状态、仓库权限和 PR 地址")
    data = json.loads(result.stdout)
    if data.get("errors") or not data["data"]["repository"]["pullRequest"]:
        raise ValueError("GitHub 评审查询未返回完整结果")
    return data["data"]["repository"]["pullRequest"]


def _eligibility(item: dict) -> str | None:
    if item.get("system") or item.get("bot"):
        return "系统消息或机器人评论"
    if item.get("resolved"):
        return "讨论已解决"
    if item.get("outdated"):
        return "评论代码版本已过期"
    if item.get("pending"):
        return "评审尚未提交"
    if not item.get("body", "").strip():
        return "评论为空"
    if not item.get("author"):
        return "缺少可追溯作者"
    if len(item["body"]) > 12000:
        return "评论过长，需人工拆分"
    return None


def github_reviews(source: dict) -> dict:
    comments, head, cursor = [], None, None
    for _ in range(10):
        page = _gh(source, cursor)
        if head and head != page["headRefOid"]:
            raise ValueError("分页读取期间 PR 版本发生变化，请重新获取")
        head = page["headRefOid"]
        for thread in page["reviewThreads"]["nodes"]:
            if thread["comments"]["pageInfo"]["hasNextPage"]:
                raise ValueError("单条讨论超过 50 条评论，不能把部分结果作为完整来源")
            for note in thread["comments"]["nodes"]:
                author, review = note.get("author") or {}, note.get("pullRequestReview") or {}
                comments.append(
                    {
                        "id": str(note["databaseId"]),
                        "body": note["body"],
                        "url": note["url"],
                        "author": author.get("login"),
                        "bot": author.get("__typename") == "Bot",
                        "path": thread["path"],
                        "line": thread["line"],
                        "side": thread["diffSide"],
                        "resolved": thread["isResolved"],
                        "outdated": thread["isOutdated"],
                        "pending": review.get("state") == "PENDING",
                        "updated_at": note["updatedAt"],
                        "review_commit": (review.get("commit") or {}).get("oid"),
                        "thread": thread["id"],
                    }
                )
        pagination = page["reviewThreads"]["pageInfo"]
        if not pagination["hasNextPage"]:
            if _gh(source)["headRefOid"] != head:
                raise ValueError("评审获取完成前 PR 版本发生变化")
            return {"head_sha": head, "state": page["state"], "comments": comments}
        cursor = pagination["endCursor"]
    raise ValueError("PR 讨论超过读取预算，需缩小评审来源")


def gitlab_reviews(source: dict) -> dict:
    configured = urlparse(os.getenv("GITLAB_BASE_URL", "https://gitlab.com"))
    if (
        configured.scheme != "https"
        or configured.hostname != source["host"]
        or configured.port
        or configured.username
    ):
        raise ValueError("MR 主机与 GITLAB_BASE_URL 不匹配")
    token = os.getenv("GITLAB_API_TOKEN", "")
    headers = {"PRIVATE-TOKEN": token} if token else {}
    endpoint = f"https://{source['host']}/api/v4/projects/{quote(source['project'], safe='')}/merge_requests/{source['number']}"
    with httpx.Client(timeout=30, follow_redirects=False, headers=headers) as client:

        def get(url, **params):
            response = client.get(url, params=params)
            if response.status_code != 200:
                raise ValueError(f"GitLab 评审读取失败，HTTP {response.status_code}")
            return response

        metadata = get(endpoint).json()
        head = metadata["sha"]
        comments = []
        for page in range(1, 11):
            response = get(endpoint + "/discussions", per_page=100, page=page)
            for discussion in response.json():
                for note in discussion["notes"]:
                    position, author = note.get("position") or {}, note.get("author") or {}
                    comments.append(
                        {
                            "id": str(note["id"]),
                            "body": note["body"],
                            "url": source["url"] + f"#note_{note['id']}",
                            "author": author.get("username"),
                            "bot": bool(author.get("bot")),
                            "system": note.get("system", False),
                            "resolved": note.get("resolved", False),
                            "outdated": bool(position and position.get("head_sha") != head),
                            "path": position.get("new_path") or position.get("old_path"),
                            "line": position.get("new_line") or position.get("old_line"),
                            "side": "RIGHT" if position.get("new_line") else "LEFT",
                            "updated_at": note.get("updated_at"),
                            "review_commit": position.get("head_sha"),
                            "thread": discussion["id"],
                        }
                    )
            if not response.headers.get("X-Next-Page"):
                if get(endpoint).json()["sha"] != head:
                    raise ValueError("分页读取期间 MR 版本发生变化")
                return {"head_sha": head, "state": metadata["state"], "comments": comments}
        raise ValueError("MR 讨论超过读取预算，不能静默截断")


def fetch_reviews(store: Store, repo: dict, url: str) -> dict:
    source = source_identity(url)
    assert_repo_source(repo, source)
    payload = github_reviews(source) if source["provider"] == "github" else gitlab_reviews(source)
    for item in payload["comments"]:
        item["skip_reason"] = _eligibility(item)
        item["eligible"] = item["skip_reason"] is None
    data = {"repo_id": repo["id"], "source": source, **payload}
    data["id"] = "reviews-" + digest(data)[:20]
    store.put("review_batch", data["id"], data)
    return data


def validate_provenance(store: Store, provenance: dict, repo: dict | None = None):
    old = store.get("review_batch", provenance["batch_id"])
    repo = repo or store.get("repo", old["repo_id"])
    if repo["id"] != old["repo_id"]:
        raise ValueError("不能使用其他仓库的评审批次")
    current = fetch_reviews(store, repo, old["source"]["url"])
    if current["id"] != old["id"]:
        raise ValueError("评审来源已变化（代码、评论或讨论状态），请重新获取并提炼")


def learn_review(
    store: Store,
    repo: dict,
    batch_id: str,
    comment_id: str,
    model,
    paths=None,
    checker=None,
    historical=False,
):
    batch = store.get("review_batch", batch_id)
    provenance = {
        "batch_id": batch_id,
        "comment_id": str(comment_id),
        "url": batch["source"]["url"],
        "head_sha": batch["head_sha"],
    }
    validate_provenance(store, provenance, repo)
    comment = next((c for c in batch["comments"] if c["id"] == str(comment_id)), None)
    accepted_history = (
        historical
        and comment
        and comment["skip_reason"] in {"讨论已解决", "评论代码版本已过期"}
        and not comment.get("bot")
        and not comment.get("system")
        and not comment.get("pending")
        and bool(comment.get("author"))
        and 0 < len(comment["body"].strip()) <= 12000
    )
    if not comment or (not comment["eligible"] and not accepted_history):
        raise ValueError("所选评论不存在或不是当前可导入的人工评审")
    if historical and not paths:
        raise ValueError("历史评审需要显式指定当前文件 --scope")
    provenance["historical"] = bool(historical)
    paths = paths or ([comment["path"]] if comment.get("path") else [])
    if not paths or any(any(c in p for c in "*?[") for p in paths):
        raise ValueError("评审学习需要具体文件作用域，不能自动扩大到整个仓库")
    for path in paths:
        if not safe_path(Path(repo["path"]), path).is_file():
            raise ValueError("评审作用域文件在当前本地仓库不存在，请显式指定映射后的文件")
    provenance.update(
        author=comment["author"], comment_url=comment["url"], updated_at=comment["updated_at"]
    )
    key = (
        "learn-"
        + digest({"repo": repo["id"], "source": provenance, "paths": paths, "checker": checker})[
            :24
        ]
    )
    record = new_learning(
        store,
        repo["id"],
        comment["body"],
        paths,
        workflow_id=key,
        provenance=provenance,
        checker=checker,
    )
    return continue_learning(store, record["id"], model)
