"""Day 07 Retrieval & Context Engineering Experiments.

Implements and compares 4 retrieval methods and 3 improvement configurations
connected to the real Day 06 PostgreSQL + pgvector store and gemini-embedding-001 (768d):

1. tfidf_b800                (Baseline - Method 1: Lexical TF-IDF, top_k=3, budget=800)
2. dense_pgvector_b800       (Method 2: Dense Retrieval on Day 6 pgvector, top_k=3, budget=800)
3. dense_pgvector_b3000      (Improvement 1: Dense pgvector + Context Budget 3000 chars)
4. hybrid_rrf_filter_b3000   (Method 3 & Improvement 2 [Selected]: Hybrid Dense pgvector + BM25 + Metadata Filter, budget=3000)
5. hybrid_window_pack_b1500  (Method 4 & Improvement 3: Hybrid + Query-Focused Sentence-Window Packing, budget=1500)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import time
import unicodedata
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv


def tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", unicodedata.normalize("NFC", text).casefold())


def hit(chunk: dict[str, Any], gold: list[dict[str, Any]]) -> bool:
    return any(
        chunk["source_id"] == g["source_id"]
        and chunk["page"] == g["page"]
        and g["evidence"] in chunk["text"]
        for g in gold
    )


def pack_context(results: list[dict[str, Any]], budget: int = 800) -> tuple[str, list[str]]:
    blocks, ids, seen = [], [], set()
    for c in results:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        block = f"[{c['id']} | {c['source']} | page {c['page']}]\n{c['text']}"
        if len("\n\n".join(blocks + [block])) <= budget:
            blocks.append(block)
            ids.append(c["id"])
    return "\n\n".join(blocks), ids


def pack_context_sentence_window(
    question: str, results: list[dict[str, Any]], budget: int = 1500, max_chunk_chars: int = 620
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Query-focused sentence-window compression: keeps the most relevant contiguous sentence
    window inside oversized chunks so top-k chunks fit inside a tighter context budget.
    """
    q_terms = set(tokenize(question))
    blocks, ids, compressed_results, seen = [], [], [], set()
    for c in results:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        text = c["text"]
        if len(text) > max_chunk_chars:
            sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
            if len(sents) > 1:
                best_idx = 0
                best_score = -1.0
                for idx, s in enumerate(sents):
                    s_terms = Counter(tokenize(s))
                    overlap = sum(1 for t in q_terms if t in s_terms)
                    if overlap > best_score:
                        best_score = overlap
                        best_idx = idx
                # Expand window around best sentence up to max_chunk_chars
                win = [sents[best_idx]]
                left, right = best_idx - 1, best_idx + 1
                while left >= 0 or right < len(sents):
                    added = False
                    if right < len(sents) and len(" ".join(win + [sents[right]])) <= max_chunk_chars:
                        win.append(sents[right])
                        right += 1
                        added = True
                    if left >= 0 and len(" ".join([sents[left]] + win)) <= max_chunk_chars:
                        win.insert(0, sents[left])
                        left -= 1
                        added = True
                    if not added:
                        break
                text = " ".join(win)
        c_mod = {**c, "text": text}
        block = f"[{c_mod['id']} | {c_mod['source']} | page {c_mod['page']}]\n{c_mod['text']}"
        if len("\n\n".join(blocks + [block])) <= budget:
            blocks.append(block)
            ids.append(c_mod["id"])
            compressed_results.append(c_mod)
        else:
            compressed_results.append(c)
    return "\n\n".join(blocks), ids, compressed_results


def infer_filter_from_query(question: str, explicit_filter: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Infer chapter/section metadata filter from user-provided filter or natural language query
    without ever accessing benchmark gold labels.
    """
    if explicit_filter:
        return dict(explicit_filter)
    q_low = question.lower()
    sec_match = re.search(r"\bsection\s+([12]\.[123])\b", q_low)
    if sec_match:
        sec = sec_match.group(1)
        chap = f"chapter_{sec.split('.')[0]}"
        return {"chapter": chap, "section": sec}
    chap_match = re.search(r"\bchapter\s+([12])\b", q_low)
    if chap_match:
        return {"chapter": f"chapter_{chap_match.group(1)}"}
    return None


class TfidfRetriever:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks
        counts = [Counter(tokenize(c["text"])) for c in chunks]
        df = Counter(term for row in counts for term in row)
        self.idf = {term: math.log((1 + len(chunks)) / (1 + count)) + 1 for term, count in df.items()}
        self.vectors = [self._vector(row) for row in counts]

    def _vector(self, counts: Counter[str]) -> dict[str, float]:
        vector = {term: count * self.idf[term] for term, count in counts.items() if term in self.idf}
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
        return {term: value / norm for term, value in vector.items()}

    def search(self, question: str, *, filters: dict[str, Any] | None = None, top_k: int = 3) -> list[dict[str, Any]]:
        query = self._vector(Counter(tokenize(question)))
        results = []
        for chunk, vector in zip(self.chunks, self.vectors):
            if filters:
                if not all(str(chunk.get("metadata", {}).get(k)) == str(v) for k, v in filters.items()):
                    continue
            score = sum(value * vector.get(term, 0.0) for term, value in query.items())
            if score > 0:
                results.append({**chunk, "score": round(float(score), 6), "strategy": "tfidf"})
        return sorted(results, key=lambda c: (-c["score"], c["id"]))[:top_k]


class GeminiQueryEmbedder:
    """Query embedder compatible with Day 6 gemini-embedding-001 (768d, L2-normalized)."""

    def __init__(self, api_key: str, cache_path: Path, model_name: str = "gemini-embedding-001", dimensions: int = 768) -> None:
        self.api_key = api_key
        self.cache_path = cache_path
        self.model_name = model_name
        self.dimensions = dimensions
        self.api_calls = 0
        self.cache: dict[str, list[float]] = {}
        if cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                self.cache = {}

    def _cache_key(self, text: str, task_type: str = "RETRIEVAL_QUERY") -> str:
        digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
        key1 = f"{self.model_name}:{self.dimensions}:{task_type}:{digest}"
        if key1 in self.cache:
            return key1
        digest_raw = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{self.model_name}:{self.dimensions}:{task_type}:{digest_raw}"

    def embed_query(self, text: str) -> list[float]:
        key = self._cache_key(text, "RETRIEVAL_QUERY")
        if key in self.cache:
            return self.cache[key]
        if not self.api_key:
            raise RuntimeError("Missing GEMINI_API_KEY and query not found in embedding cache.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:embedContent?key={self.api_key}"
        payload = {
            "model": f"models/{self.model_name}",
            "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_QUERY",
            "outputDimensionality": self.dimensions,
        }
        body = json.dumps(payload).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(8):
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as e:
                last_err = e
                time.sleep(3.0 + attempt * 4.0)
        else:
            raise RuntimeError(f"embed_query failed after retries: {last_err}")
        vals = [float(x) for x in data["embedding"]["values"]]
        norm = math.sqrt(sum(v * v for v in vals)) or 1.0
        vec = [round(v / norm, 8) for v in vals]
        self.api_calls += 1
        self.cache[key] = vec
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
        return vec


class Day6PgVectorRetriever:
    """Dense retrieval directly querying the Day 6 PostgreSQL + pgvector database (`chunks` table)."""

    def __init__(self, database_url: str, embedder: GeminiQueryEmbedder, strategy_label: str = "C") -> None:
        self.database_url = database_url
        self.embedder = embedder
        self.strategy_label = strategy_label

    def search(self, question: str, *, filters: dict[str, Any] | None = None, top_k: int = 3) -> list[dict[str, Any]]:
        q_vec = self.embedder.embed_query(question)
        vec_literal = "[" + ",".join(f"{v:.8f}" for v in q_vec) + "]"

        where_clauses = ["strategy = %s"]
        params: list[Any] = [vec_literal, self.strategy_label]

        if filters:
            for k, v in filters.items():
                if k in ("chapter", "section", "topic", "document_id"):
                    col = "document_id" if k == "document_id" else k
                    where_clauses.append(f"{col} = %s")
                    params.append(str(v))
                else:
                    where_clauses.append("metadata ->> %s = %s")
                    params.extend([str(k), str(v)])

        params.extend([vec_literal, top_k])
        where_sql = " AND ".join(where_clauses)
        sql = f"""
            SELECT chunk_id, document_id, source, page, text, metadata,
                   1 - (embedding <=> %s::vector) AS cosine_similarity
            FROM document_chunks
            WHERE {where_sql}
            ORDER BY embedding <=> %s::vector ASC, chunk_id ASC
            LIMIT %s;
        """
        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        results = []
        for r in rows:
            meta = r[5] if isinstance(r[5], dict) else json.loads(r[5])
            results.append(
                {
                    "id": r[0],
                    "source_id": r[1],
                    "source": r[2],
                    "page": int(r[3]),
                    "text": r[4],
                    "metadata": meta,
                    "score": round(float(r[6]), 6),
                    "strategy": "dense_pgvector",
                }
            )
        return results


class HybridPgVectorBm25Retriever:
    """Method 3: Hybrid Retrieval combining Day 6 pgvector Dense similarity + BM25 lexical score
    via normalized score fusion & Reciprocal Rank Fusion (RRF), with optional metadata filtering.
    """

    def __init__(
        self,
        chunks: list[dict[str, Any]],
        pg_retriever: Day6PgVectorRetriever,
        alpha: float = 0.75,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self.chunks = chunks
        self.chunk_by_id = {c["id"]: c for c in chunks}
        self.pg_retriever = pg_retriever
        self.alpha = alpha
        self.k1 = k1
        self.b = b

        self.doc_tokens = {c["id"]: tokenize(c["text"]) for c in chunks}
        self.doc_lens = {cid: len(toks) for cid, toks in self.doc_tokens.items()}
        self.avgdl = sum(self.doc_lens.values()) / len(self.doc_lens) if self.doc_lens else 1.0
        self.df: Counter[str] = Counter()
        for toks in self.doc_tokens.values():
            for t in set(toks):
                self.df[t] += 1
        self.n_docs = len(chunks)

    def _bm25_scores(self, query: str, candidate_ids: list[str]) -> dict[str, float]:
        q_toks = tokenize(query)
        scores: dict[str, float] = {}
        for cid in candidate_ids:
            tf = Counter(self.doc_tokens[cid])
            dl = self.doc_lens[cid]
            s = 0.0
            for term in q_toks:
                n_q = self.df.get(term, 0)
                if n_q == 0:
                    continue
                idf = math.log(1.0 + (self.n_docs - n_q + 0.5) / (n_q + 0.5))
                f = tf.get(term, 0)
                if f > 0:
                    s += idf * ((f * (self.k1 + 1.0)) / (f + self.k1 * (1.0 - self.b + self.b * (dl / self.avgdl))))
            scores[cid] = s
        return scores

    def search(self, question: str, *, filters: dict[str, Any] | None = None, top_k: int = 3) -> list[dict[str, Any]]:
        # Retrieve candidate pool from pgvector (up to 40 candidates matching filter)
        dense_candidates = self.pg_retriever.search(question, filters=filters, top_k=min(len(self.chunks), 40))
        if not dense_candidates:
            return []

        cand_ids = [c["id"] for c in dense_candidates]
        dense_map = {c["id"]: c["score"] for c in dense_candidates}
        bm25_map = self._bm25_scores(question, cand_ids)

        max_d, min_d = max(dense_map.values()), min(dense_map.values())
        span_d = (max_d - min_d) if max_d > min_d else 1.0
        max_b = max(bm25_map.values()) if bm25_map else 0.0

        dense_ranks = {cid: rank + 1 for rank, cid in enumerate(sorted(cand_ids, key=lambda x: -dense_map[x]))}
        bm25_ranks = {cid: rank + 1 for rank, cid in enumerate(sorted(cand_ids, key=lambda x: -bm25_map[x]))}

        fused = []
        for c in dense_candidates:
            cid = c["id"]
            d_norm = (dense_map[cid] - min_d) / span_d
            b_norm = (bm25_map[cid] / max_b) if max_b > 0 else 0.0
            rrf = (1.0 / (60.0 + dense_ranks[cid])) + (1.0 / (60.0 + bm25_ranks[cid]))
            hybrid_score = self.alpha * d_norm + (1.0 - self.alpha) * b_norm + 0.1 * rrf
            fused.append(
                {
                    **c,
                    "score": round(float(hybrid_score), 6),
                    "dense_score": round(float(dense_map[cid]), 6),
                    "bm25_score": round(float(bm25_map[cid]), 4),
                    "strategy": "hybrid_rrf",
                }
            )
        fused.sort(key=lambda x: (-x["score"], x["id"]))
        return fused[:top_k]


def run_config(
    config_name: str,
    description: str,
    retriever: Any,
    questions: list[dict[str, Any]],
    *,
    top_k: int = 3,
    budget: int = 800,
    use_filter: bool = False,
    use_window_pack: bool = False,
    output_dir: Path,
) -> dict[str, Any]:
    traces = []
    for q in questions:
        start = time.perf_counter()
        flt = infer_filter_from_query(q["question"], q.get("filter")) if use_filter else None
        results = retriever.search(q["question"], filters=flt, top_k=top_k)
        if use_window_pack:
            context, ids, packed_results = pack_context_sentence_window(q["question"], results, budget=budget)
            marked_context_hits = any(hit(c, q["gold"]) and c["id"] in ids for c in packed_results)
        else:
            context, ids = pack_context(results, budget=budget)
            marked_context_hits = any(hit(c, q["gold"]) and c["id"] in ids for c in results)

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        marked = [{**c, "hit": hit(c, q["gold"])} for c in results[:3]]
        traces.append(
            {
                "query_id": q["id"],
                "split": q["split"],
                "question": q["question"],
                "strategy": config_name,
                "description": description,
                "top_k": top_k,
                "budget_chars": budget,
                "applied_filter": flt,
                "gold": q["gold"],
                "results": marked,
                "context": context,
                "context_ids": ids,
                "context_chars": len(context),
                "elapsed_ms": round(elapsed_ms, 3),
                "hit1": bool(marked and marked[0]["hit"]),
                "hit3": any(c["hit"] for c in marked),
                "context_hit": bool(marked_context_hits),
            }
        )

    split = questions[0]["split"] if questions else "dev"
    n = len(traces)
    summary = {
        "strategy": config_name,
        "description": description,
        "split": split,
        "n": n,
        "hit1": sum(t["hit1"] for t in traces),
        "hit1_rate": round(sum(t["hit1"] for t in traces) / n, 4) if n else 0.0,
        "hit3": sum(t["hit3"] for t in traces),
        "hit3_rate": round(sum(t["hit3"] for t in traces) / n, 4) if n else 0.0,
        "context_hit": sum(t["context_hit"] for t in traces),
        "context_hit_rate": round(sum(t["context_hit"] for t in traces) / n, 4) if n else 0.0,
        "avg_context_chars": round(statistics.mean(t["context_chars"] for t in traces), 1) if n else 0.0,
        "median_ms": round(statistics.median(t["elapsed_ms"] for t in traces), 2) if n else 0.0,
        "service_cost_usd": 0.0,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "traces.json").write_text(json.dumps(traces, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Day 07 retrieval & context benchmark experiments")
    parser.add_argument("--chunks", type=Path, default=Path("starter/data/converted/chunks.json"))
    parser.add_argument("--questions", type=Path, default=Path("starter/data/converted/questions.json"))
    parser.add_argument("--results-dir", type=Path, default=Path("starter/results"))
    parser.add_argument("--day6-dir", type=Path, default=Path("../K5-L3A-DAY06-BuiGiaHuy-2A202603525-DataIngestion"))
    args = parser.parse_args()

    d6_env = args.day6_dir / "starter" / ".env"
    if d6_env.exists():
        load_dotenv(d6_env)
    load_dotenv()

    database_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:55433/day6_lab")
    api_key = os.getenv("GEMINI_API_KEY", "")
    cache_path = args.day6_dir / "starter" / "results" / ".embedding_cache.json"

    chunks = json.loads(args.chunks.read_text(encoding="utf-8"))
    questions = json.loads(args.questions.read_text(encoding="utf-8"))
    dev_qs = [q for q in questions if q["split"] == "dev"]
    test_qs = [q for q in questions if q["split"] == "test"]

    tfidf_ret = TfidfRetriever(chunks)
    embedder = GeminiQueryEmbedder(api_key=api_key, cache_path=cache_path)
    pg_ret = Day6PgVectorRetriever(database_url=database_url, embedder=embedder, strategy_label="C")
    hybrid_ret = HybridPgVectorBm25Retriever(chunks=chunks, pg_retriever=pg_ret, alpha=0.75)

    all_summaries = []

    # --- DEV EXPERIMENTS ---
    all_summaries.append(
        run_config(
            "1_baseline_tfidf_b800",
            "Method 1 (Baseline): Lexical TF-IDF, top_k=3, no filter, budget=800 chars",
            tfidf_ret,
            dev_qs,
            top_k=3,
            budget=800,
            use_filter=False,
            output_dir=args.results_dir / "dev-tfidf-b800",
        )
    )
    all_summaries.append(
        run_config(
            "2_dense_pgvector_b800",
            "Method 2: Dense Retrieval on Day 6 pgvector (gemini-embedding-001 768d), top_k=3, no filter, budget=800 chars",
            pg_ret,
            dev_qs,
            top_k=3,
            budget=800,
            use_filter=False,
            output_dir=args.results_dir / "dev-dense-pgvector-b800",
        )
    )
    all_summaries.append(
        run_config(
            "3_improve1_dense_pgvector_b3000",
            "Improvement 1: Dense pgvector + Expand Context Budget to 3000 chars (fixes Dev Error #1 chunk drop)",
            pg_ret,
            dev_qs,
            top_k=3,
            budget=3000,
            use_filter=False,
            output_dir=args.results_dir / "dev-dense-pgvector-b3000",
        )
    )
    all_summaries.append(
        run_config(
            "4_method3_improve2_hybrid_rrf_filter_b3000",
            "Method 3 & Improvement 2 (Selected): Hybrid (Dense pgvector + BM25 + RRF) + Metadata Filter, top_k=3, budget=3000 chars",
            hybrid_ret,
            dev_qs,
            top_k=3,
            budget=3000,
            use_filter=True,
            output_dir=args.results_dir / "dev-hybrid-rrf-filter-b3000",
        )
    )
    all_summaries.append(
        run_config(
            "5_method4_improve3_hybrid_window_pack_b1500",
            "Method 4 & Improvement 3: Hybrid + Query-Focused Sentence-Window Compression under tight budget=1500 chars",
            hybrid_ret,
            dev_qs,
            top_k=3,
            budget=1500,
            use_filter=True,
            use_window_pack=True,
            output_dir=args.results_dir / "dev-hybrid-window-pack-b1500",
        )
    )

    # --- LOCKED TEST EVALUATION (Baseline vs Selected Config) ---
    all_summaries.append(
        run_config(
            "test_baseline_tfidf_b800",
            "Baseline on Test: Lexical TF-IDF, top_k=3, no filter, budget=800 chars",
            tfidf_ret,
            test_qs,
            top_k=3,
            budget=800,
            use_filter=False,
            output_dir=args.results_dir / "test-tfidf-b800",
        )
    )
    all_summaries.append(
        run_config(
            "test_selected_hybrid_rrf_filter_b3000",
            "Selected Config on Test: Hybrid (Dense pgvector + BM25 + RRF) + Metadata Filter, top_k=3, budget=3000 chars",
            hybrid_ret,
            test_qs,
            top_k=3,
            budget=3000,
            use_filter=True,
            output_dir=args.results_dir / "test-hybrid-rrf-filter-b3000",
        )
    )

    (args.results_dir / "retrieval_comparison_summary.json").write_text(
        json.dumps(all_summaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(all_summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
