
import asyncio
from typing import List, Dict, Any, AsyncIterator

import openai

from prompt import SYSTEM_PROMPT
from config import MODEL_NAME, TEMPERATURE, MAX_TOKENS, MAX_PASSAGE_CHARS
from tool_calling import TOOLS, execute_tool_call

MAX_TOOL_ROUNDS = 3
TOOL_PROBE_MAX_TOKENS = 300  # intermediate rounds only need a short answer or a tool-call JSON

_CONTEXT_OVERFLOW_MESSAGE = (
    "দুঃখিত, প্রশ্ন ও প্রাসঙ্গিক তথ্য একসঙ্গে মডেলের সর্বোচ্চ ধারণক্ষমতার চেয়ে বড় হয়ে গেছে। "
    "একটু ছোট করে বা নির্দিষ্ট করে আবার জিজ্ঞাসা করুন।"
)

# ── Client singleton ────────────────────────────────────────────────────────

_client: openai.AsyncOpenAI | None = None


def get_client(base_url: str, api_key: str = "not-needed") -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
    return _client


# ── Prompt builder 


def build_prompt(
    query: str,
    passages: List[Dict[str, Any]],
    history: List[Dict[str, str]] | None = None,
) -> List[Dict[str, str]]:
    if not passages:
        context_block = "No relevant passages found."
    else:
        parts = []
        for i, p in enumerate(passages, 1):
            meta = f"[Passage {i} | Category: {p['category']} | Topic: {p['topic']}]"
            answer = p["answer"]
            if len(answer) > MAX_PASSAGE_CHARS:
                answer = answer[:MAX_PASSAGE_CHARS].rstrip() + "…"
            parts.append(f"{meta}\n{answer}")
            if p.get("url") and p["url"] not in ("nan", ""):
                parts.append(f"Source: {p['url']}")
        context_block = "\n\n---\n\n".join(parts)

    user_message = (
        f"Context:\n{context_block}\n\n"
        f"Question: {query}\n\n"
        "Answer based on the context above:"
    )

    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return messages


# ── Query contextualization ─────────────────────────────────────────────────

_CONTEXTUALIZE_PROMPT = (
    "Given the conversation history and the latest user message, "
    "if the latest message is a short follow-up or refers to something in the history, "
    "rewrite it as a complete, self-contained search query. "
    "If it is already a full standalone question, return it unchanged. "
    "Output ONLY the rewritten query — no explanation, no extra text."
)

async def contextualize_query(
    query: str,
    history: List[Dict[str, str]],
    base_url: str,
    model: str = MODEL_NAME,
) -> str:
    if not history:
        return query
    client = get_client(base_url)
    messages = [{"role": "system", "content": _CONTEXTUALIZE_PROMPT}]
    messages.extend(history[-4:])   # last 2 turns is enough
    messages.append({"role": "user", "content": query})
    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        max_tokens=128,
    )
    return response.choices[0].message.content.strip()


# ── Generation (non-streaming) ──────────────────────────────────────────────

async def generate(
    query: str,
    passages: List[Dict[str, Any]],
    base_url: str,
    model: str = MODEL_NAME,
    temperature: float = 0.05,
    max_tokens: int = 1024,
    history: List[Dict[str, str]] | None = None,
) -> str:
    client = get_client(base_url)
    messages = build_prompt(query, passages, history)

    response = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content


# ── Generation with tool calling (non-streaming) ────────────────────────────


async def generate_with_tools(
    query: str,
    passages: List[Dict[str, Any]],
    base_url: str,
    model: str = MODEL_NAME,
    temperature: float = 0.05,
    max_tokens: int = 1024,
    history: List[Dict[str, str]] | None = None,
) -> str:
    client = get_client(base_url)
    messages = build_prompt(query, passages, history)

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=TOOL_PROBE_MAX_TOKENS,
                tools=TOOLS,
            )
            choice = response.choices[0].message
            if not choice.tool_calls:
                print("[tool_calling] no tool_calls in response — model answered directly")
                break

            print(f"[tool_calling] model requested {len(choice.tool_calls)} tool call(s): "
                  f"{[tc.function.name + '(' + tc.function.arguments + ')' for tc in choice.tool_calls]}")
            messages.append(choice.model_dump(exclude_none=True))
            for tool_call in choice.tool_calls:
                result = await execute_tool_call(tool_call)
                print(f"[tool_calling] {tool_call.function.name} result: {result[:300]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
        else:
            messages.append({
                "role": "user",
                "content": "Please answer now with the information gathered so far.",
            })

        # Final answer, with the full requested token budget.
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content
    except openai.BadRequestError as exc:
        print(f"[tool_calling] context overflow: {exc}")
        return _CONTEXT_OVERFLOW_MESSAGE


# ── Generation with tool calling (streaming) ────────────────────────────────


async def generate_stream_with_tools(
    query: str,
    passages: List[Dict[str, Any]],
    base_url: str,
    model: str = MODEL_NAME,
    temperature: float = 0.05,
    max_tokens: int = 1024,
    history: List[Dict[str, str]] | None = None,
) -> AsyncIterator[str]:
    client = get_client(base_url)
    messages = build_prompt(query, passages, history)

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=TOOL_PROBE_MAX_TOKENS,
                tools=TOOLS,
            )
            choice = response.choices[0].message
            if not choice.tool_calls:
                print("[tool_calling] no tool_calls in response — model answered directly")
                break

            print(f"[tool_calling] model requested {len(choice.tool_calls)} tool call(s): "
                  f"{[tc.function.name + '(' + tc.function.arguments + ')' for tc in choice.tool_calls]}")
            messages.append(choice.model_dump(exclude_none=True))
            for tool_call in choice.tool_calls:
                result = await execute_tool_call(tool_call)
                print(f"[tool_calling] {tool_call.function.name} result: {result[:300]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
        else:
            messages.append({
                "role": "user",
                "content": "Please answer now with the information gathered so far.",
            })

        # Re-issue the resolved conversation with stream=True so the caller gets
        # real token-by-token output for the final answer.
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
    except openai.BadRequestError as exc:
        print(f"[tool_calling] context overflow: {exc}")
        yield _CONTEXT_OVERFLOW_MESSAGE


# ── Generation (streaming)

async def generate_stream(
    query: str,
    passages: List[Dict[str, Any]],
    base_url: str,
    model: str = MODEL_NAME,
    temperature: float = 0.05,
    max_tokens: int = 1024,
    history: List[Dict[str, str]] | None = None,
) -> AsyncIterator[str]:
    client = get_client(base_url)
    messages = build_prompt(query, passages, history)

    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta
