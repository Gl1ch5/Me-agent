"""
name: github
description: GitHub issues, PRs and repos via REST API
icon: ◉
color: magenta
env: GITHUB_TOKEN
---
Needs secret GITHUB_TOKEN (/secret). repo is "owner/name".
Use list_* before acting; comment/create need user intent.
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
