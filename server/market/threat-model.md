---
name: threat-model
description: a repo-grounded threat model: assets, entry points, trust boundaries, attacker capabilities, ranked abuse paths and fixes tied to files. Use when the user asks how an app could be attacked or before a security review
icon: ◈
color: red
---
Method adapted from OpenAI's security-threat-model skill (github.com/openai/skills). Defensive use on code the user owns or is authorized to assess.

1. Scope: what system, which version/branch, deployed how (local tool, SaaS, mobile, internal). Read the code, configs, Dockerfiles, CI, infra files.
2. Inventory from the code (cite files): assets (user data, secrets, money, admin rights, compute), entry points (HTTP routes, webhooks, CLIs, file uploads, message queues, plugins), trust boundaries (browser↔server, server↔DB, service↔third party, tenant↔tenant), authn/authz points.
3. Attacker model: who (anonymous internet user, logged-in user, another tenant, malicious dependency, insider), what they can do, and what they explicitly cannot.
4. Abuse paths: a short list (5-12), each a concrete story from entry point to impact — injection (SQL/command/template/prompt), broken access control/IDOR, SSRF, path traversal, deserialization, auth/session flaws, secrets in repo/logs, supply chain, DoS by unbounded input.
5. Rate each: likelihood and impact (low/med/high) with one-line reasons; mark what you verified in code versus assumed.
6. Before writing the final report, ask the user 1-3 questions that would change the ratings (e.g. "is this admin panel internet-facing?").
7. Write THREAT_MODEL.md: summary table, the paths ranked, mitigations tied to file:line with the smallest effective fix first, and what to test. Don't change code unless asked.
