import asyncio
import json
from typing import Any, Dict, List

from ddgs import DDGS

# ── Tool schema (OpenAI-compatible function-calling format) ────────────────

TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live web via DuckDuckGo. Use this only when the question "
                "needs current or up-to-date information that the government knowledge "
                "base passages don't cover (e.g. recent news, a fee/rule that may have "
                "changed, something outside the local dataset)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 3, max 5).",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
    }
]


# ── Tool implementation ─────────────────────────────────────────────────────


MAX_SNIPPET_CHARS = 300


def web_search(query: str, max_results: int = 3) -> List[Dict[str, str]]:
    max_results = max(1, min(max_results, 5))
    with DDGS() as ddgs:
        results = ddgs.text(query, max_results=max_results)
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href") or r.get("url", ""),
            "snippet": r.get("body", "")[:MAX_SNIPPET_CHARS],
        }
        for r in results
    ]


_TOOL_FUNCTIONS = {"web_search": web_search}


async def execute_tool_call(tool_call) -> str:
    """Run one OpenAI-style tool call and return its JSON string result."""
    name = tool_call.function.name
    try:
        arguments = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError:
        arguments = {}

    fn = _TOOL_FUNCTIONS.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"})

    try:
        result = await asyncio.to_thread(fn, **arguments)
    except Exception as exc:
        result = {"error": str(exc)}

    return json.dumps(result, ensure_ascii=False)
