
import asyncio
from typing import List, Dict, Any, AsyncIterator

import openai

from prompt import SYSTEM_PROMPT
from config import MODEL_NAME, TEMPERATURE, MAX_TOKENS

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
            parts.append(f"{meta}\n{p['answer']}")
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
