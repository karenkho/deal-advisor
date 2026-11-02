"""Corpus loading, chunking, embedding cache and hybrid (dense + BM25) search with RRF."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams
from rank_bm25 import BM25Okapi

from app import faults, llm, tracing
from app.config import CACHE_DIR, CORPUS_DIR

FEATURE_TYPES = ["screen_crud", "workflow", "integration", "report_dashboard",
                 "data_migration", "notification", "admin_config", "nfr"]
EMBED_CACHE = CACHE_DIR / "embeddings.json"
PROPOSAL_EMBED_CACHE = CACHE_DIR / "embeddings_proposal.json"
COLLECTION = "corpus"
RRF_K = 60


@dataclass
class Chunk:
    id: str
    text: str
    metadata: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


# ---------- parsing ----------

def _split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body
    return {}, text


def _sections(body: str) -> list[tuple[str, str]]:
    """Split on '## ' headings; returns (heading, content)."""
    parts = re.split(r"^## +(.+)$", body, flags=re.M)
    return [(parts[i].strip(), parts[i + 1].strip()) for i in range(1, len(parts), 2)]


def parse_project(path: Path) -> list[Chunk]:
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    base = {k: fm[k] for k in ("doc_id", "project_id", "domain", "project_size", "months", "title") if k in fm}
    chunks: list[Chunk] = []
    epic = None
    for line in body.splitlines():
        m = re.match(r"^### Epic: (.+)$", line)
        if m:
            epic = m.group(1).strip()
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[1] in FEATURE_TYPES:
            feature, ftype, size, ac1, ac2 = cells
            n = len([c for c in chunks if c.metadata.get("kind") == "feature"]) + 1
            text = (f"Past project {base['project_id']} ({base['title']}, {base['domain']}, "
                    f"{base['project_size']}, {base['months']} months). Epic: {epic}. "
                    f"Feature: {feature}. Type: {ftype}. Size: {size}.\nAC: {ac1}\nAC: {ac2}")
            chunks.append(Chunk(f"{base['project_id']}-F{n:02d}", text,
                                {**base, "kind": "feature", "doc_type": "project", "epic": epic,
                                 "feature": feature, "feature_type": ftype, "size": size}))
    for heading, content in _sections(body):
        if heading in ("TOR summary", "Foundation", "Assumptions", "Exclusions"):
            chunks.append(Chunk(f"{base['project_id']}-{heading.replace(' ', '_')}",
                                f"Past project {base['project_id']} ({base['title']}). {heading}:\n{content}",
                                {**base, "kind": heading.lower().replace(" ", "_"), "doc_type": "project"}))
    return chunks


def parse_sectioned(path: Path) -> list[Chunk]:
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    doc_type = fm.get("doc_type", path.stem)
    chunks = []
    for heading, content in _sections(body):
        meta = {"doc_id": fm.get("doc_id", path.stem), "doc_type": doc_type, "kind": doc_type, "heading": heading}
        m = re.match(r"Sizing rubric: (\w+)", heading)
        if m:
            meta["kind"] = "rubric"
            meta["feature_type"] = m.group(1)
        slug = re.sub(r"[^a-z0-9]+", "_", heading.lower()).strip("_")
        chunks.append(Chunk(f"{doc_type}-{slug}", f"{heading}\n{content}", meta))
    return chunks


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> list[Chunk]:
    chunks: list[Chunk] = []
    for name in ("guideline.md", "patterns.md", "glossary.md"):
        chunks.extend(parse_sectioned(corpus_dir / name))
    for path in sorted((corpus_dir / "projects").glob("project_*.md")):
        chunks.extend(parse_project(path))
    return chunks


# ---------- embedding cache ----------

def _load_cache(path: Path) -> dict[str, list[float]]:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def embed_with_cache(chunks: list[Chunk], usage: llm.Usage | None = None,
                     cache_path: Path = EMBED_CACHE) -> tuple[np.ndarray, dict]:
    """Embed only chunks whose content hash is not cached. Returns vectors and stats."""
    cache = _load_cache(cache_path)
    missing = [c for c in chunks if c.content_hash not in cache]
    if missing:
        vectors = llm.embed([c.text for c in missing], usage=usage, node="corpus_embed")
        for c, v in zip(missing, vectors):
            cache[c.content_hash] = v
    live = {c.content_hash for c in chunks}
    pruned = {h: v for h, v in cache.items() if h in live}
    if missing or len(pruned) != len(cache):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(pruned))
    stats = {"chunks": len(chunks), "embedded": len(missing), "cached": len(chunks) - len(missing),
             "pruned": len(cache) - len(pruned)}
    return np.array([pruned[c.content_hash] for c in chunks], dtype=np.float32), stats


# ---------- index + hybrid search ----------

def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


_FILLER = set("a an the and or of to in on for with is are was were be been how what which when where why who does do did can could should would may might will this that these those it its as by at from about within into their them they you your please explain mean means".split())


def _content_tokens(text: str) -> list[str]:
    return [w for w in _tokenize(text) if w not in _FILLER]


class CorpusIndex:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray):
        self.chunks = chunks
        self.by_id = {c.id: c for c in chunks}
        self.bm25 = BM25Okapi([_tokenize(c.text) for c in chunks])
        self.qdrant = QdrantClient(":memory:")
        self.qdrant.create_collection(COLLECTION, vectors_config=VectorParams(size=vectors.shape[1], distance=Distance.COSINE))
        self.qdrant.upsert(COLLECTION, points=[
            PointStruct(id=i, vector=vectors[i].tolist(), payload={**c.metadata, "chunk_id": c.id})
            for i, c in enumerate(chunks)])

    @classmethod
    def build(cls, usage: llm.Usage | None = None) -> tuple["CorpusIndex", dict]:
        chunks = load_corpus()
        vectors, stats = embed_with_cache(chunks, usage)
        return cls(chunks, vectors), stats

    @tracing.traced("retrieve", lambda self, query, k=5, filters=None, usage=None: {"query": query, "k": k, "filters": filters or {}},
                    lambda hits: {"documents": [{"id": c.id, "doc_type": c.metadata.get("doc_type"), "score": round(float(score), 4)} for c, score in hits]}, "retriever")
    def search(self, query: str, k: int = 5, filters: dict | None = None, usage: llm.Usage | None = None) -> list[tuple[Chunk, float]]:
        """Hybrid search: dense (Qdrant) + sparse (BM25) fused with reciprocal rank fusion. RETRIEVAL_MODE=dense is the evaluation baseline."""
        if faults.active("empty_retrieval"):
            return []
        filters = filters or {}
        allowed = [i for i, c in enumerate(self.chunks) if all(c.metadata.get(f) == v for f, v in filters.items())]
        if not allowed:
            return []
        depth = min(len(allowed), max(k * 4, 20))
        qvec = llm.embed([query], usage=usage, node="query_embed")[0]
        qfilter = Filter(must=[FieldCondition(key=f, match=MatchValue(value=v)) for f, v in filters.items()]) if filters else None
        dense = self.qdrant.query_points(COLLECTION, query=qvec, limit=depth, query_filter=qfilter).points
        dense_rank = [p.id for p in dense]
        if os.environ.get("RETRIEVAL_MODE", "hybrid") == "dense":
            return [(self.chunks[p.id], p.score) for p in dense[:k]]
        if os.environ.get("RETRIEVAL_MODE") == "hybrid_tuned":     # evaluation only, not yet production: see docs/evaluation/rag_results.md
            self._bm25_content = getattr(self, "_bm25_content", None) or BM25Okapi([_content_tokens(c.text) for c in self.chunks])
            scores = self._bm25_content.get_scores(_content_tokens(query))
        else:
            scores = self.bm25.get_scores(_tokenize(query))
        sparse_rank = sorted(allowed, key=lambda i: scores[i], reverse=True)[:depth]
        fused: dict[int, float] = {}
        for ranking in (dense_rank, sparse_rank):
            for rank, idx in enumerate(ranking):
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        top = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [(self.chunks[i], s) for i, s in top]
