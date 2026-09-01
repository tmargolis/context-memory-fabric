"""Read-only acceptance test against configured local LLM_Wiki.

Runs a full read-only scan, calculates statistics, and executes demonstration
searches across Markdown, Reports/Output, PDF, and Media/Binary assets.
"""

from collections import Counter
import json
import os
from pathlib import Path
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.corpus import ExtractionStatus, MatchBasis, get_corpus_root
from server.wiki import CorpusScanner, CorpusSearchEngine, scan_corpus


def run_acceptance():
    print("=================================================================")
    print(" Phase 1 Step 6A: LLM_Wiki Real Corpus Read-Only Acceptance Test ")
    print("=================================================================")

    wiki_root = get_corpus_root()
    print(f"\n1. Validated Corpus Root: {wiki_root}")

    # Record directory modification time before scan
    before_mtime = wiki_root.stat().st_mtime

    print("\n2. Scanning and extracting corpus assets (READ-ONLY)...")
    scanner = CorpusScanner(root_path=wiki_root)
    assets = scanner.scan(extract_content=True)

    # Verify no modification occurred
    after_mtime = wiki_root.stat().st_mtime
    assert before_mtime == after_mtime, "Root directory modification time changed!"

    total_assets = len(assets)
    print(f"\nTotal Discovered Non-Ignored Assets: {total_assets}")

    # Breakdown by media/file type
    ext_counts = Counter(a.extension for a in assets)
    media_counts = Counter(a.media_type for a in assets)
    status_counts = Counter(a.extraction_status for a in assets)
    area_counts = Counter(a.top_level_area for a in assets)

    print("\n--- Breakdown by Extension ---")
    for ext, count in ext_counts.most_common():
        print(f"  {ext:<15} : {count:>5}")

    print("\n--- Breakdown by Extraction Status ---")
    for status, count in status_counts.most_common():
        print(f"  {status:<35} : {count:>5}")

    print("\n--- Breakdown by Top-Level Area ---")
    for area, count in area_counts.most_common():
        print(f"  {area:<25} : {count:>5}")

    print("\n--- Representative Examples by Area ---")
    seen_areas = set()
    for asset in assets:
        if asset.top_level_area not in seen_areas:
            seen_areas.add(asset.top_level_area)
            print(f"  [{asset.top_level_area:<15}] {asset.relative_path} ({asset.media_type}, status={asset.extraction_status})")

    # Index into Search Engine
    engine = CorpusSearchEngine(assets)

    print("\n=================================================================")
    print(" Demonstration Searches (Read-Only)                              ")
    print("=================================================================")

    queries_to_demo = [
        ("Markdown Content Search", "Personal-Context-Service"),
        ("Reports / Output Area Search", "Margolis"),
        ("PDF Embedded Text Search", "Candidate"),
        ("Media Asset Search by Filename/Path", "pluto"),
        ("Audio Asset Search by Filename/Path", "strainbrain"),
    ]

    for label, query in queries_to_demo:
        print(f"\n>>> Query: '{query}' ({label})")
        results = engine.search(query, max_results=3)
        if not results:
            print("  (No results found)")
            continue
        for idx, res in enumerate(results, 1):
            print(f"  [{idx}] {res.relative_path}")
            print(f"      Area: {res.top_level_area} | Media: {res.media_type}")
            print(f"      Extractor: {res.extractor} | Status: {res.extraction_status}")
            print(f"      Match Basis: {res.match_basis} | Score: {res.relevance_score}")
            if res.matched_snippet:
                print(f"      Snippet: {res.matched_snippet}")

    print("\n=================================================================")
    print(" Acceptance Verification Completed Successfully                  ")
    print("=================================================================")


if __name__ == "__main__":
    run_acceptance()
