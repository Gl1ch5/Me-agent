"""
name: github
description: GitHub issues, PRs and repos via REST API
icon: ◉
color: magenta
env: GITHUB_TOKEN
---
Needs secret GITHUB_TOKEN (/secret). repo is "owner/name".
Use list_* before acting; comment/create need user intent.
git clone/push to github.com work in bash with the same token (no setup needed). Flow: clone → branch → commit → git push -u origin <branch> → open_pr.
"""
API = "https://api.github.com"


def _h(ctx):
    return {"Authorization": "Bearer " + ctx.env.get("GITHUB_TOKEN", ""), "Accept": "application/vnd.github+json"}


@action
def list_prs(ctx, repo: str, state="open"):
    "List pull requests"
    ctx.status(f"fetching PRs of {repo}")
    return [{"n": p["number"], "title": p["title"], "by": p["user"]["login"]} for p in ctx.http.get(f"{API}/repos/{repo}/pulls?state={state}", _h(ctx))]


@action
def list_issues(ctx, repo: str, state="open"):
    "List issues"
    return [{"n": i["number"], "title": i["title"]} for i in ctx.http.get(f"{API}/repos/{repo}/issues?state={state}", _h(ctx)) if "pull_request" not in i]


@action(confirm=True)
def create_issue(ctx, repo: str, title: str, body=""):
    "Create an issue"
    r = ctx.http.post(f"{API}/repos/{repo}/issues", {"title": title, "body": body}, _h(ctx))
    return r["html_url"]


@action(confirm=True)
def comment(ctx, repo: str, number: int, body: str):
    "Comment on an issue/PR"
    return ctx.http.post(f"{API}/repos/{repo}/issues/{number}/comments", {"body": body}, _h(ctx))["html_url"]


@action(confirm=True)
def open_pr(ctx, repo: str, head: str, title: str, base="main", body=""):
    "Open a pull request from branch head into base (push the branch first with git)"
    r = ctx.http.post(f"{API}/repos/{repo}/pulls", {"title": title, "head": head, "base": base, "body": body}, _h(ctx))
    return r["html_url"]


@action
def repos(ctx, query=""):
    "Your repositories (most recently pushed first)"
    rs = ctx.http.get(f"{API}/user/repos?sort=pushed&per_page=50", _h(ctx))
    return [{"repo": r["full_name"], "private": r["private"], "pushed": r["pushed_at"][:10]} for r in rs if query.lower() in r["full_name"].lower()]
