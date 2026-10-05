"""Replay and evaluation (MS8).

Rebuild the context that was available at a past moment, rerun historical
questions against it, and compare retrieval policies on the same cases,
without writing to production memory.

- snapshot.py: point-in-time copies of the episodic graph and the wiki
- policies.py:  retrieval policies as named, reversible settings
- grading.py:   per-case grades (gold hit, rank, temporal leak, provenance, ...)
- runner.py:    run cases x snapshots x policies, export trajectories
"""
