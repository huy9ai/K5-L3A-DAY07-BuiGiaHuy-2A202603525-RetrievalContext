"""Generate Day 07 benchmark files:
1. day7_bundle/data/converted/chunks.json (113 chunks from Day 6 Strategy C)
2. day7_bundle/data/converted/questions.json (20 answerable questions: 15 dev, 5 test)
3. day7_bundle/data/agent_cases.json (24 agent benchmark cases: 17 dev [15 ans + 2 unans], 7 test [5 ans + 2 unans])
"""
from __future__ import annotations

import json
from pathlib import Path


def main() -> None:
    repo_d6 = Path(__file__).resolve().parents[2]
    raw_chunks = json.loads((repo_d6 / "results" / "day6_export_chunks.json").read_text(encoding="utf-8"))
    raw_questions = json.loads((repo_d6 / "results" / "day6_export_questions.json").read_text(encoding="utf-8"))

    chunks = [
        {
            "id": c.get("id", c.get("chunk_id")),
            "source_id": c.get("source_id", c.get("document_id")),
            "source": c.get("source"),
            "page": c.get("page"),
            "text": c.get("text"),
            "metadata": c.get("metadata", {}),
        }
        for c in raw_chunks
    ]

    questions = []
    for q in raw_questions:
        gold = q.get("gold")
        if gold is None:
            gold = [
                {
                    "source_id": q.get("gold_source_id"),
                    "page": q.get("gold_page"),
                    "evidence": q.get("evidence_quote"),
                }
            ]
        item = {
            "id": q["id"],
            "split": q["split"],
            "question": q["question"],
            "gold": gold,
            "expected_answer": q["expected_answer"],
        }
        if q.get("filter"):
            item["filter"] = q["filter"]
        questions.append(item)

    unanswerable_cases = [
        {
            "id": "u01",
            "split": "dev",
            "question": "According to Chapter 1 of the textbook, what was the exact clock frequency in GHz and retail launch price in USD of the Intel Core i9 processor when it was released?",
            "answerable": False,
            "expected_behavior": "search_and_abstain",
            "absence_reason": "Checked all 37 valid pages (P019-P042, P049-P061): Chapter 1 discusses historical computers (Difference Engine, ENIAC, IBM PC with Mark Dean) and general hardware concepts (CPU, memory, storage, I/O), but never mentions Intel Core i9 clock frequencies or retail prices.",
        },
        {
            "id": "u02",
            "split": "dev",
            "question": "In Section 2.1 on Computational Thinking, what mathematical learning rate schedule and dropout probability does the textbook prescribe for training a Transformer language model?",
            "answerable": False,
            "expected_behavior": "search_and_abstain",
            "absence_reason": "Checked all 13 valid pages of Chapter 2 Section 2.1 (P049-P061): Section 2.1 covers decomposition, pattern recognition, abstraction, the Three As, algorithms, and DNA sequencing k-mers, but contains zero information on Transformer hyperparameters, learning rate schedules, or dropout probabilities.",
        },
        {
            "id": "u03",
            "split": "test",
            "question": "What exact civil fine in dollars did the court impose on the nine Washington, D.C. housing companies in the 2021 Facebook targeted-advertising lawsuit discussed in Section 1.3?",
            "answerable": False,
            "expected_behavior": "search_and_abstain",
            "absence_reason": "Checked Page 41 (Section 1.3): the textbook states that in 2021 a Facebook user filed a class-action lawsuit arguing nine companies in the Washington, D.C. area excluded people over age 50 from housing ads as an example of digital redlining, but it never states the court verdict or any dollar fine amount.",
        },
        {
            "id": "u04",
            "split": "test",
            "question": "According to Section 1.2, what minimum GPA and tuition fee are required to enroll in the OpenStax Certified Ethical Hacking degree program?",
            "answerable": False,
            "expected_behavior": "search_and_abstain",
            "absence_reason": "Checked all pages of Section 1.2 (P029-P036) and the entire 37-page corpus: Section 1.2 discusses computer science across disciplines, spreadsheets, and cybersecurity/Y2K, and does not mention any OpenStax Certified Ethical Hacking degree program, GPA requirement, or tuition fee.",
        },
    ]

    agent_cases = []
    for q in questions:
        case = {
            "id": q["id"],
            "split": q["split"],
            "question": q["question"],
            "answerable": True,
            "expected_behavior": "search_and_answer",
            "expected_answer": q["expected_answer"],
            "gold": q["gold"],
        }
        if "filter" in q:
            case["filter"] = q["filter"]
        agent_cases.append(case)

    agent_cases.extend(unanswerable_cases)

    bundle_root = repo_d6 / "day7_bundle"
    conv_dir = bundle_root / "starter" / "data" / "converted"
    data_dir = bundle_root / "data"
    conv_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    (conv_dir / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (conv_dir / "questions.json").write_text(json.dumps(questions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (data_dir / "agent_cases.json").write_text(json.dumps(agent_cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    dev_cases = [c for c in agent_cases if c["split"] == "dev"]
    test_cases = [c for c in agent_cases if c["split"] == "test"]
    print(f"Generated {len(chunks)} chunks, {len(questions)} answerable questions, and {len(agent_cases)} agent cases:")
    print(f"  - dev:  {len(dev_cases)} total ({sum(c['answerable'] for c in dev_cases)} answerable + {sum(not c['answerable'] for c in dev_cases)} unanswerable)")
    print(f"  - test: {len(test_cases)} total ({sum(c['answerable'] for c in test_cases)} answerable + {sum(not c['answerable'] for c in test_cases)} unanswerable)")


if __name__ == "__main__":
    main()
