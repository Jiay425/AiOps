"""Execute checked-in scenarios; this is a scenario gate, not method-level 1:1 proof."""

from __future__ import annotations

import asyncio
import json

from ops_autoagent.api import _evaluate_cases, _ops_fixture_summary, _run_rag_evaluation, store


async def main() -> int:
    await store.initialize()
    try:
        codeops = await _evaluate_cases(None)
        ops = await _ops_fixture_summary(None)
        rag = await _run_rag_evaluation("HYBRID_RAG")
    finally:
        await store.close()
    result = {"codeops": {"total": codeops["totalCases"], "passed": codeops["successCases"]},
              "ops": {key: ops[key] for key in ("totalCases", "successCases", "failedCases",
                                                  "averageEvidenceCoverage", "averageExpectedToolCoverage")},
              "runbookRag": {key: rag[key] for key in ("totalCases", "successCases", "failedCases", "top1Recall",
                                                            "top3Recall", "top5Recall", "meanReciprocalRank")}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    complete = (codeops["successCases"] == codeops["totalCases"]
                and ops["successCases"] == ops["totalCases"]
                and ops["averageEvidenceCoverage"] == 1
                and ops["averageExpectedToolCoverage"] == 1
                and rag["failedCases"] == 0)
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
