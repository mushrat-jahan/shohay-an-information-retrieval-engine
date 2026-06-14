
import re
import unicodedata
import time
from typing import List, Dict, Any, Optional

import numpy as np
import pandas as pd
import torch
from sentence_transformers import SentenceTransformer
from turbovec import IdMapIndex

from config import EMBED_MODEL


# ── Constants ─────────────────────────────────────────────────────────────────

DIM        = 4096   
BIT_WIDTH  = 4      
BATCH_SIZE = 4      

QUERY_TASK = (
    "Given a user question about Bangladeshi government services, "
    "retrieve the most relevant passage that answers it."
)


# ── Text helpers ──────────────────────────────────────────────────────────────

def normalise(text: str) -> str:
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_int(val, fallback: int) -> int:
    try:
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return fallback


def _format_query(query: str) -> str:
    # e5-mistral requires task instruction prefix on queries only
    return f"Instruct: {QUERY_TASK}\nQuery: {query}"


def _format_document(doc: Dict[str, Any]) -> str:
    parts = [doc.get("topic", ""), doc.get("service", ""), doc.get("answer", "")[:1500]]
    return "\n".join(p for p in parts if p).strip()


# ── Document builder ──────────────────────────────────────────────────────────

def build_documents(df: pd.DataFrame) -> List[Dict[str, Any]]:
    docs = []
    for _, row in df.iterrows():
        docs.append({
            "id":           _safe_int(row.get("Passage ID"), len(docs)),
            "category":     normalise(str(row.get("Category", ""))),
            "sub_category": normalise(str(row.get("Sub-Category", ""))),
            "service":      normalise(str(row.get("Service", ""))),
            "topic":        normalise(str(row.get("Topic", ""))),
            "answer":       str(row.get("Text", "")),
            "url":          str(row.get("URL", "")),
        })
    return docs


# ── TurboVec Retriever ────────────────────────────────────────────────────────

class TurboVecRetriever:
    """
    Dense retriever backed by turbovec IdMapIndex.

    Public interface:
        retriever.docs                   — list of doc dicts
        retriever.retrieve(q, top_k=5)   — returns list of doc dicts with 'score'
    """

    def __init__(self, docs: List[Dict[str, Any]], model_name: str):
        self.docs = docs
        self._slot_to_doc: Dict[int, Dict[str, Any]] = {i: d for i, d in enumerate(docs)}
        self._index: Optional[IdMapIndex] = None

        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[TurboVec] Loading {model_name} on {device} …")
        self._model = SentenceTransformer(model_name, device=device)
        print("[TurboVec] Model ready.")

    def build_index(self, index_path: Optional[str] = None) -> None:
        """Embed all passages and build the turbovec index. Runs once at startup."""

        if index_path:
            try:
                self._index = IdMapIndex.load(index_path)
                print(f"[TurboVec] Loaded cached index from {index_path} "
                      f"({len(self.docs)} passages)")
                return
            except Exception:
                print(f"[TurboVec] No cached index at {index_path}, building…")

        texts = [_format_document(d) for d in self.docs]
        print(f"[TurboVec] Embedding {len(texts)} passages (batch={BATCH_SIZE}) …")
        t0 = time.time()

        vectors = self._model.encode(
            texts,
            batch_size=BATCH_SIZE,
            normalize_embeddings=True,
            show_progress_bar=True,
        ).astype(np.float32)

        print(f"[TurboVec] Done in {time.time()-t0:.1f}s | shape={vectors.shape}")

        slots = np.arange(len(self.docs), dtype=np.uint64)
        self._index = IdMapIndex(dim=DIM, bit_width=BIT_WIDTH)
        self._index.add_with_ids(vectors, slots)
        self._index.prepare()
        print(f"[TurboVec] Index ready — {len(self.docs)} vectors, {BIT_WIDTH}-bit quantised")

        if index_path:
            self._index.write(index_path)
            print(f"[TurboVec] Index saved to {index_path}")

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        if self._index is None:
            raise RuntimeError("Index not built — call build_index() first")

        q_vec = self._model.encode(
            [_format_query(query)],
            normalize_embeddings=True,
        ).astype(np.float32)

        scores, slot_ids = self._index.search(q_vec, k=top_k)

        results = []
        for score, slot in zip(scores[0], slot_ids[0]):
            doc = self._slot_to_doc.get(int(slot))
            if doc is None:
                continue
            result = doc.copy()
            result["score"] = float(score)
            results.append(result)
        return results


# ── Factory ───────────────────────────────────────────────────────────────────

def load_retriever(
    csv_path:   str,
    model_name: str           = EMBED_MODEL,
    index_path: Optional[str] = None,
) -> TurboVecRetriever:
    df   = pd.read_csv(csv_path).dropna(subset=["Text"])
    docs = build_documents(df)

    retriever = TurboVecRetriever(docs, model_name=model_name)
    retriever.build_index(index_path=index_path)
    return retriever
