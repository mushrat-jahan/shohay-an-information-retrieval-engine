import json
from contextlib import asynccontextmanager
from typing import List, Optional
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel
from retriever import load_retriever, TurboVecRetriever
from generator import generate, generate_stream, contextualize_query
from config import ( CSV_PATH, VLLM_URL, MODEL_NAME, EMBED_MODEL, INDEX_PATH, TOP_K, TEMPERATURE, MAX_TOKENS,)
from fastapi.staticfiles import StaticFiles

retriever: TurboVecRetriever | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global retriever
    print(f"[Startup] Building turbovec index from {CSV_PATH} …")
    print(f"[Startup] Embed : {EMBED_MODEL} (in-process)")
    print(f"[Startup] Generate: {MODEL_NAME} @ {VLLM_URL}")
    retriever = load_retriever(
        csv_path   = CSV_PATH,
        model_name = EMBED_MODEL,
        index_path = INDEX_PATH,
    )
    print("[Startup] Ready.")
    yield
    print("[Shutdown] Bye.")


app = FastAPI(title="Bengali QA RAG API", version="2.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="fonts"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response schemas 

class Message(BaseModel):
    role: str       # "user" or "assistant"
    content: str


class QueryRequest(BaseModel):
    query: str
    top_k: Optional[int] = None
    stream: Optional[bool] = False
    history: Optional[List[Message]] = []


class PassageInfo(BaseModel):
    id: int
    category: str
    sub_category: str
    topic: str
    score: float
    url: str


class QueryResponse(BaseModel):
    query:str
    answer:str
    passages: List[PassageInfo]


# ── Endpoints 

@app.get("/health")
def health():
    return {
        "status": "ok",
        "passages_loaded": len(retriever.docs) if retriever else 0,
        "index_backend": "turbovec (4-bit, in-process)",
        "embed_model": EMBED_MODEL,
        "gen_model": MODEL_NAME,
    }


@app.post("/query", response_model=QueryResponse)
async def query_endpoint(req: QueryRequest):
    if retriever is None:
        raise HTTPException(503, "Retriever not ready")

    k = req.top_k or TOP_K
    history = [m.model_dump() for m in req.history] if req.history else None

    retrieval_query = await contextualize_query(req.query, history or [], VLLM_URL)
    passages = retriever.retrieve(retrieval_query, top_k=k)

    answer = await generate(
        query = req.query,
        passages = passages,
        base_url = VLLM_URL,
        model = MODEL_NAME,
        temperature = TEMPERATURE,
        max_tokens = MAX_TOKENS,
        history = history,
    )

    return QueryResponse(
        query    = req.query,
        answer   = answer,
        passages = [
            PassageInfo(
                id = p["id"],
                category = p["category"],
                sub_category = p["sub_category"],
                topic = p["topic"],
                score = round(p["score"], 4),
                url = p["url"],
            )
            for p in passages
        ],
    )


@app.post("/query/stream")
async def query_stream_endpoint(req: QueryRequest):
    if retriever is None:
        raise HTTPException(503, "Retriever not ready")

    k = req.top_k or TOP_K
    history = [m.model_dump() for m in req.history] if req.history else None

    retrieval_query = await contextualize_query(req.query, history or [], VLLM_URL)
    passages = retriever.retrieve(retrieval_query, top_k=k)

    passage_meta = json.dumps([
        {
            "id": p["id"], "category": p["category"],
            "sub_category": p["sub_category"], "topic": p["topic"],
            "score": round(p["score"], 4), "url": p["url"],
        }
        for p in passages
    ])

    async def token_generator():
        yield passage_meta + "\n---END_PASSAGES---\n"
        async for token in generate_stream(
            query = req.query,
            passages = passages,
            base_url = VLLM_URL,
            model = MODEL_NAME,
            temperature = TEMPERATURE,
            max_tokens = MAX_TOKENS,
            history = history,
        ):
            yield token
    return StreamingResponse(token_generator(), media_type="text/plain")


@app.get("/passages/search")
async def search_passages(q: str, top_k: int = 5):
    """Debug: raw turbovec retrieval without generation."""
    if retriever is None:
        raise HTTPException(503, "Retriever not ready")
    passages = retriever.retrieve(q, top_k=top_k)
    return {"query": q, "results": passages}


BASE_DIR = Path(__file__).resolve().parent
@app.get("/", response_class=HTMLResponse)
def chat_ui():
    with open(BASE_DIR / "index.html", encoding="utf-8") as f:
        return f.read()
