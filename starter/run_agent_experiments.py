"""Run Day 07 Agentic RAG experiments on dev (17 cases) and test (7 cases).

Compares:
1. Baseline Agent (`tfidf_b800`): Lexical TF-IDF retriever, top_k=3, budget=800 chars, max_searches=2
2. Selected Agent (`hybrid_rrf_filter_b3000`): Hybrid (Day 6 pgvector Dense + BM25 + RRF) + Inferred Filter, top_k=3, budget=3000 chars, max_searches=2

Outputs for each run (`agent-dev-baseline`, `agent-dev-selected`, `agent-test-baseline`, `agent-test-selected`):
- `traces.json`     (compatible with `starter/agent_metrics.py`)
- `reviews.json`    (detailed grading labels + rationale per query & citation)
- `summary.json`    (official metrics aggregated via `starter/agent_metrics.py`)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent_metrics import aggregate
from agentic import GeminiController, run_agent
from run_retrieval_experiments import (
    Day6PgVectorRetriever,
    GeminiQueryEmbedder,
    HybridPgVectorBm25Retriever,
    TfidfRetriever,
    hit,
    infer_filter_from_query,
    tokenize,
)


def grade_context_and_answer(
    case: dict[str, Any],
    trace: dict[str, Any],
    chunk_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Produce structured review record for `agent_metrics.py` plus context grade (0/1/2)
    and human-readable rationale for the report table.
    """
    qid = case["id"]
    answerable = bool(case["answerable"])
    status = trace.get("status", "error")
    citations = trace.get("citations", [])
    events = trace.get("events", [])
    search_events = [e for e in events if e.get("event") == "search"]
    final_context = search_events[-1].get("context", "") if search_events else ""
    final_context_ids = search_events[-1].get("context_ids", []) if search_events else []

    if not answerable:
        # Unanswerable question: must abstain without fabricating facts
        abstention_correct = status == "abstain"
        citation_support = [False for _ in citations]
        reason = (
            f"Câu thiếu bằng chứng ({case.get('absence_reason', '')[:90]}...). "
            + (
                "Agent đã gọi search kiểm tra kho dữ liệu và từ chối trả lời đúng (abstain), không bịa thông tin."
                if abstention_correct
                else "Agent không từ chối mà tự suy diễn câu trả lời không có trong nguồn."
            )
        )
        return {
            "query_id": qid,
            "answerable": False,
            "context_grade": 0,
            "abstention_correct": abstention_correct,
            "citation_support": citation_support,
            "rationale": reason,
        }

    # Answerable question
    gold_list = case.get("gold", [])
    context_chunks = [chunk_by_id[cid] for cid in final_context_ids if cid in chunk_by_id]
    has_gold_in_context = any(hit(c, gold_list) for c in context_chunks)

    if has_gold_in_context:
        context_grade = 2
    elif final_context_ids:
        context_grade = 1
    else:
        context_grade = 0

    # Check whether each cited chunk actually contains gold evidence or comes from the gold page
    gold_pages = {g["page"] for g in gold_list}
    citation_support: list[bool] = []
    for cid in citations:
        c_obj = chunk_by_id.get(cid)
        if not c_obj:
            citation_support.append(False)
        else:
            is_supporting = hit(c_obj, gold_list) or (c_obj["page"] in gold_pages and has_gold_in_context)
            citation_support.append(bool(is_supporting))

    if status == "abstain":
        answer_grade = 0
        reason = (
            f"Từ chối nhầm câu có đáp án (context_grade={context_grade}): "
            + (
                "Do budget=800 ký tự làm rớt chunk chứa bằng chứng (>800 ký tự) khỏi context."
                if not has_gold_in_context
                else "Có bằng chứng trong context nhưng model từ chối."
            )
        )
    else:
        ans_text = trace.get("answer", "")
        exp_tokens = set(tokenize(case.get("expected_answer", "")))
        ans_tokens = set(tokenize(ans_text))
        overlap_ratio = len(exp_tokens & ans_tokens) / max(len(exp_tokens), 1)
        if any(citation_support) and overlap_ratio >= 0.35:
            answer_grade = 2
            reason = f"Trả lời đúng và đủ các ý chính, trích dẫn {citations} khớp trang nguồn {sorted(gold_pages)}."
        elif any(citation_support) or overlap_ratio >= 0.25:
            answer_grade = 1
            reason = f"Trả lời đúng một phần ý chính (overlap={overlap_ratio:.2f}), trích dẫn {citations}."
        else:
            answer_grade = 0
            reason = f"Câu trả lời hoặc trích dẫn {citations} không chứa bằng chứng chuẩn ở trang {sorted(gold_pages)}."

    return {
        "query_id": qid,
        "answerable": True,
        "context_grade": context_grade,
        "answer_grade": answer_grade,
        "citation_support": citation_support,
        "rationale": reason,
    }


def execute_agent_suite(
    suite_name: str,
    cases: list[dict[str, Any]],
    controller: GeminiController,
    retriever: Any,
    chunk_by_id: dict[str, dict[str, Any]],
    *,
    top_k: int,
    budget: int,
    max_searches: int,
    use_filter: bool,
    output_dir: Path,
) -> dict[str, Any]:
    traces = []
    reviews = []
    calls_before = controller.llm_calls
    ptok_before = controller.prompt_tokens
    ctok_before = controller.completion_tokens

    for case in cases:
        qid = case["id"]
        question = case["question"]
        flt = infer_filter_from_query(question, case.get("filter")) if use_filter else None
        t0 = time.perf_counter()
        try:
            res = run_agent(
                question,
                controller,
                retriever,
                filters=flt,
                max_searches=max_searches,
                top_k=top_k,
                budget=budget,
            )
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            num_searches = sum(1 for e in res.get("events", []) if e.get("event") == "search")
            trace_item = {
                "query_id": qid,
                "split": case["split"],
                "question": question,
                "answerable": case["answerable"],
                "applied_filter": flt,
                "status": res["status"],
                "answer": res["answer"],
                "citations": res["citations"],
                "num_searches": num_searches,
                "elapsed_ms": elapsed_ms,
                "events": res["events"],
            }
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            trace_item = {
                "query_id": qid,
                "split": case["split"],
                "question": question,
                "answerable": case["answerable"],
                "applied_filter": flt,
                "status": "error",
                "error": str(e),
                "answer": "",
                "citations": [],
                "num_searches": 0,
                "elapsed_ms": elapsed_ms,
                "events": [],
            }

        traces.append(trace_item)
        if trace_item["status"] != "error":
            reviews.append(grade_context_and_answer(case, trace_item, chunk_by_id))

    agg = aggregate(cases, traces, reviews)
    search_counts = [t["num_searches"] for t in traces if t["status"] != "error"]
    ctx_grades_ans = [r["context_grade"] for r in reviews if r.get("answerable")]
    llm_calls_used = controller.llm_calls - calls_before
    ptok_used = controller.prompt_tokens - ptok_before
    ctok_used = controller.completion_tokens - ctok_before
    # Gemini 2.5 Flash standard pricing estimate ($0.15 / 1M input, $0.60 / 1M output; $0 on Free Tier)
    est_cost_usd = round((ptok_used * 0.15 + ctok_used * 0.60) / 1_000_000, 6)

    summary = {
        "suite": suite_name,
        "split": cases[0]["split"] if cases else "dev",
        "budget_chars": budget,
        "top_k": top_k,
        "max_searches": max_searches,
        "use_filter": use_filter,
        **agg,
        "avg_searches_per_case": round(statistics.mean(search_counts), 2) if search_counts else 0.0,
        "search_count_distribution": {
            str(k): sum(1 for x in search_counts if x == k) for k in sorted(set(search_counts))
        },
        "context_grade_distribution_answerable": {
            "0": sum(1 for g in ctx_grades_ans if g == 0),
            "1": sum(1 for g in ctx_grades_ans if g == 1),
            "2": sum(1 for g in ctx_grades_ans if g == 2),
            "N": len(ctx_grades_ans),
            "mean": round(sum(ctx_grades_ans) / len(ctx_grades_ans), 4) if ctx_grades_ans else 0.0,
        },
        "median_ms": round(statistics.median(t["elapsed_ms"] for t in traces), 2) if traces else 0.0,
        "llm_api_calls": llm_calls_used,
        "prompt_tokens": ptok_used,
        "completion_tokens": ctok_used,
        "estimated_cost_usd": est_cost_usd,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "traces.json").write_text(json.dumps(traces, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "reviews.json").write_text(json.dumps(reviews, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Day 07 Agentic RAG evaluation on dev and test")
    parser.add_argument("--chunks", type=Path, default=Path("starter/data/converted/chunks.json"))
    parser.add_argument("--cases", type=Path, default=Path("data/agent_cases.json"))
    parser.add_argument("--results-dir", type=Path, default=Path("starter/results"))
    parser.add_argument("--day6-dir", type=Path, default=Path("../K5-L3A-DAY06-BuiGiaHuy-2A202603525-DataIngestion"))
    args = parser.parse_args()

    d6_env = args.day6_dir / "starter" / ".env"
    if d6_env.exists():
        load_dotenv(d6_env)
    load_dotenv()

    database_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:55433/day6_lab")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY in ../K5-L3A-DAY06-BuiGiaHuy-2A202603525-DataIngestion/starter/.env")

    emb_cache_path = args.day6_dir / "starter" / "results" / ".embedding_cache.json"
    llm_cache_path = args.results_dir / ".agent_llm_cache.json"

    chunks = json.loads(args.chunks.read_text(encoding="utf-8"))
    chunk_by_id = {c["id"]: c for c in chunks}
    all_cases = json.loads(args.cases.read_text(encoding="utf-8"))
    dev_cases = [c for c in all_cases if c["split"] == "dev"]
    test_cases = [c for c in all_cases if c["split"] == "test"]

    tfidf_ret = TfidfRetriever(chunks)
    embedder = GeminiQueryEmbedder(api_key=api_key, cache_path=emb_cache_path)
    pg_ret = Day6PgVectorRetriever(database_url=database_url, embedder=embedder, strategy_label="C")
    hybrid_ret = HybridPgVectorBm25Retriever(chunks=chunks, pg_retriever=pg_ret, alpha=0.75)

    controller = GeminiController(
        api_key=api_key,
        model_name="gemini-3.8-flash",
        cache_path=llm_cache_path,
        max_searches_hint=2,
    )

    summaries = []
    print("1/4 Running Baseline Agent on DEV (17 cases, TF-IDF, budget=800)...")
    summaries.append(
        execute_agent_suite(
            "agent_dev_baseline_tfidf_b800",
            dev_cases,
            controller,
            tfidf_ret,
            chunk_by_id,
            top_k=3,
            budget=800,
            max_searches=2,
            use_filter=False,
            output_dir=args.results_dir / "agent-dev-baseline",
        )
    )

    print("2/4 Running Selected Agent on DEV (17 cases, Hybrid pgvector+BM25+Filter, budget=3000)...")
    summaries.append(
        execute_agent_suite(
            "agent_dev_selected_hybrid_b3000",
            dev_cases,
            controller,
            hybrid_ret,
            chunk_by_id,
            top_k=3,
            budget=3000,
            max_searches=2,
            use_filter=True,
            output_dir=args.results_dir / "agent-dev-selected",
        )
    )

    print("3/4 Running Baseline Agent on TEST (7 cases, TF-IDF, budget=800)...")
    summaries.append(
        execute_agent_suite(
            "agent_test_baseline_tfidf_b800",
            test_cases,
            controller,
            tfidf_ret,
            chunk_by_id,
            top_k=3,
            budget=800,
            max_searches=2,
            use_filter=False,
            output_dir=args.results_dir / "agent-test-baseline",
        )
    )

    print("4/4 Running Selected Agent on TEST (7 cases, Hybrid pgvector+BM25+Filter, budget=3000)...")
    summaries.append(
        execute_agent_suite(
            "agent_test_selected_hybrid_b3000",
            test_cases,
            controller,
            hybrid_ret,
            chunk_by_id,
            top_k=3,
            budget=3000,
            max_searches=2,
            use_filter=True,
            output_dir=args.results_dir / "agent-test-selected",
        )
    )

    (args.results_dir / "agent_comparison_summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("\n=== AGENT EVALUATION SUMMARY ===")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
