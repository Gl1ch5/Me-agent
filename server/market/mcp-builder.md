---
name: mcp-builder
description: build a good MCP (Model Context Protocol) server in Python (FastMCP) or TypeScript: tool design, schemas, errors, transports, testing with MCP Inspector, and connecting it to me. Use when the user wants to expose an API or a tool to agents
icon: ⧉
color: cyan
---
Method adapted from Anthropic's mcp-builder skill (Apache-2.0, github.com/anthropics/skills).

## 1. Research (now, versions move)
- Read the current MCP spec and SDK README (modelcontextprotocol.io, the python-sdk / typescript-sdk repos) and the target API's docs: auth, rate limits, pagination, main resources.
- Stack: TypeScript SDK or Python FastMCP (`pip install "mcp[cli]"`). Transport: stdio for local tools; streamable HTTP (stateless JSON) for remote servers.

## 2. Design the tools for an agent, not for a REST client
- Name with a prefix and a verb: `github_create_issue`, `shop_list_orders`. One clear job per tool; merge chained calls the agent would always make together.
- Inputs: typed schemas (Pydantic / Zod) with descriptions and an example; specific names (`user_id`, not `id`).
- Outputs: focused and paginated (limit/cursor); a `response_format` of concise vs detailed when results can be big; never dump 50 KB.
- Errors that teach: say what was wrong and the next step ("no repo 'x' — call github_list_repos to see names").
- Annotations: readOnlyHint, destructiveHint, idempotentHint, openWorldHint. Destructive tools take explicit confirmation-worthy arguments.
- Secrets from env vars, never in code or output.

## 3. Build (Python sketch)
```python
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("shop")

@mcp.tool()
def shop_list_orders(status: str = "open", limit: int = 20, cursor: str = "") -> dict:
    """List orders by status, newest first. Returns {orders, next_cursor}."""
    ...

if __name__ == "__main__":
    mcp.run()  # stdio
```

## 4. Test
- `npx @modelcontextprotocol/inspector python server.py` (or `node build/index.js`): list tools, call each with good and bad input.
- Write ~10 realistic, read-only questions whose answers need several tool calls and can be checked as exact strings; run them through an agent and fix the tools that confused it.

## 5. Connect to me
Add it to me's config `mcp` section (or `/mcp` in the terminal): `{"mcp": {"shop": {"command": "python", "args": ["server.py"], "description": "orders and customers"}}}`. me loads it lazily: one line in the prompt until the mcp tool is used.
