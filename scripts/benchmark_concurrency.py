"""Benchmark concurrency levels for wiki relationship extraction against local LLM server.

Tests various concurrency levels (e.g. 1, 2, 3, 4, 5, 6) on a fixed set of sections
with dry_run=True to determine optimal throughput without mutating graph state.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.core.config import load_config
from server.providers.memory_graphiti import get_graphiti_for_operation
from server.providers.wiki.corpus import get_corpus_root

from scripts.extract_wiki_relationships import (
    parse_markdown_sections,
    process_section,
    resolve_note_dates,
    PILOT_NOTES,
)

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark_concurrency")


async def run_benchmark(concurrencies: list[int], num_sections: int = 6) -> None:
    os.environ["FALKORDB_DATABASE"] = "fixgraph-p4"
    os.environ["CMF_EXTRACTION_PROFILE"] = "typed-recall"
    os.environ["CMF_ENTITY_DEBRIS_FILTER"] = "1"

    wiki_root = get_corpus_root()
    graphiti, model_name = get_graphiti_for_operation()

    print(f"=== Concurrency Benchmark for Wiki Extraction ===")
    print(f"Model: {model_name}")
    print(f"Server Base URL: {graphiti.llm_client.config.base_url if hasattr(graphiti.llm_client, 'config') else 'default'}")
    print(f"Testing Concurrencies: {concurrencies}")
    print(f"Sections per test: {num_sections}")
    print("--------------------------------------------------\n")

    # Pick fixed sections from pilot notes
    candidate_tasks = []
    for np in PILOT_NOTES:
        full_path = wiki_root / np
        if not full_path.exists():
            continue
        dates = resolve_note_dates(wiki_root, np)
        text = full_path.read_text(encoding="utf-8", errors="replace")
        sections = parse_markdown_sections(text)
        pslug = "jspace" if "J-Space" in np else ("cityscapes" if "Cityscape" in np else "misc")
        for sec in sections:
            candidate_tasks.append((sec, np, dates, pslug))
            if len(candidate_tasks) >= num_sections:
                break
        if len(candidate_tasks) >= num_sections:
            break

    test_tasks = candidate_tasks[:num_sections]
    print(f"Selected {len(test_tasks)} sample sections for testing:")
    for idx, (sec, np, _, _) in enumerate(test_tasks, 1):
        print(f"  {idx}. [{Path(np).name}] {sec['clean_heading']} ({sec['words']} words)")
    print("\nStarting runs...\n")

    results = []

    for c in concurrencies:
        print(f"--> Testing concurrency = {c} ...", end="", flush=True)
        sem = asyncio.Semaphore(c)
        t0 = time.monotonic()

        coros = [
            process_section(
                graphiti,
                sec,
                np,
                dates,
                pslug,
                dry_run=True,
                sem=sem,
            )
            for sec, np, dates, pslug in test_tasks
        ]

        batch_results = await asyncio.gather(*coros, return_exceptions=True)
        elapsed = time.monotonic() - t0

        # Count edges and errors
        total_edges = 0
        errors = 0
        for res in batch_results:
            if isinstance(res, Exception):
                errors += 1
            elif isinstance(res, list):
                total_edges += len(res)

        sec_per_min = (len(test_tasks) / elapsed) * 60.0
        print(f" Done in {elapsed:.1f}s ({sec_per_min:.2f} sections/min, {total_edges} edges, {errors} errors)")

        results.append({
            "concurrency": c,
            "elapsed": elapsed,
            "sec_per_min": sec_per_min,
            "edges": total_edges,
            "errors": errors,
        })

        # Cooldown between tests to let server settle
        await asyncio.sleep(2.0)

    # Summary table
    base_time = results[0]["elapsed"] if results else 1.0
    print("\n==========================================================================")
    print("CONCURRENCY BENCHMARK RESULTS")
    print("==========================================================================")
    print(f"{'Concurrency':<12} | {'Elapsed (s)':<12} | {'Throughput (sec/min)':<22} | {'Speedup':<10} | {'Errors':<8}")
    print("--------------------------------------------------------------------------")
    for r in results:
        speedup = base_time / r["elapsed"] if r["elapsed"] > 0 else 0
        print(f"{r['concurrency']:<12} | {r['elapsed']:<12.1f} | {r['sec_per_min']:<22.2f} | {speedup:<10.2f}x | {r['errors']:<8}")
    print("==========================================================================\n")

    best = max(results, key=lambda x: x["sec_per_min"])
    print(f"Optimal concurrency: {best['concurrency']} ({best['sec_per_min']:.2f} sections/min, {base_time / best['elapsed']:.2f}x vs C=1)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrencies", default="1,2,3,4,5,6", help="Comma-separated concurrency levels")
    parser.add_argument("--num-sections", type=int, default=6, help="Number of sections to test per level")
    args = parser.parse_args()

    c_levels = [int(x.strip()) for x in args.concurrencies.split(",") if x.strip()]
    asyncio.run(run_benchmark(c_levels, args.num_sections))


if __name__ == "__main__":
    main()
