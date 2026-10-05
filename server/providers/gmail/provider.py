"""GmailKnowledgeProvider: email as a second knowledge source (MS5).

Reads a local Gmail snapshot (server.providers.gmail.snapshot) and answers
server.core.protocols.KnowledgeSource queries with BM25 over subject + body.
Each result carries its own provenance: the message id (document_id), the
thread link (uri), the send date (source_timestamp and source_version; a sent
message never changes), and `private:gmail:<account>` as its access scope,
so an email and a wiki note that disagree stay separately attributable.

Enabled by configuration alone, no core change:

    CMF_KNOWLEDGE_PROVIDERS=server.providers.wiki.provider:FileKnowledgeProvider,server.providers.gmail.provider:GmailKnowledgeProvider
    CMF_GMAIL_SNAPSHOT_DIR=/path/to/snapshot
    CMF_GMAIL_ACCOUNT=me@example.com   # optional; else the manifest's account

Read-only: it has no propose_change, so propose_knowledge_change reports
that email doesn't accept proposals. Email is evidence rather than curated
knowledge (docs/plan-active.md MS5), so results are what was *said*, dated,
never what is *true*.
"""

from __future__ import annotations

from collections import Counter
import math
import os
from pathlib import Path
import re
from typing import Optional

from dotenv import load_dotenv

from server.core.errors import ProviderConfigurationError
from server.core.models import KnowledgeResult
from server.providers.gmail.snapshot import MailMessage, read_snapshot

_WORD = re.compile(r"[a-z0-9][a-z0-9'_-]*")
_STOP = frozenset(
    "a an and are as at be but by for from has have i if in into is it its me my of on or our so "
    "that the their them then there this to was we were what when which will with you your".split()
)
_K1, _B, _SUBJECT_WEIGHT = 1.4, 0.75, 3
_EXCERPT_CHARS = 280


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOP]


class _Index:
    def __init__(self, messages: list[MailMessage]) -> None:
        self.messages = messages
        self.docs = [Counter(_tokens(m.subject) * _SUBJECT_WEIGHT + _tokens(m.body)) for m in messages]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(messages)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, terms: list[str], k: int) -> list[tuple[float, MailMessage]]:
        scored = []
        for m, doc, length in zip(self.messages, self.docs, self.lengths):
            score = 0.0
            for t in set(terms):
                tf = doc.get(t, 0)
                if tf:
                    score += self.idf[t] * tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * length / (self.avg or 1)))
            if score > 0:
                scored.append((score, m))
        # Ties broken by recency, then id, so ordering is stable across calls.
        scored.sort(key=lambda sm: (-sm[0], -(sm[1].timestamp.timestamp() if sm[1].timestamp else 0), sm[1].id))
        return scored[:k]


def _excerpt(body: str, terms: list[str]) -> str:
    text = " ".join(body.split())
    if len(text) <= _EXCERPT_CHARS:
        return text
    lowered, best, best_hits = text.lower(), 0, -1
    for start in range(0, len(text) - _EXCERPT_CHARS + 1, 40):
        window = lowered[start:start + _EXCERPT_CHARS]
        hits = sum(window.count(t) for t in terms)
        if hits > best_hits:
            best, best_hits = start, hits
    snippet = text[best:best + _EXCERPT_CHARS]
    return ("…" if best else "") + snippet + ("…" if best + _EXCERPT_CHARS < len(text) else "")


class GmailKnowledgeProvider:
    """server.core.protocols.KnowledgeSource over a local Gmail snapshot."""

    name = "gmail"

    def __init__(self, snapshot_dir: Optional[str] = None, account: Optional[str] = None) -> None:
        load_dotenv()
        self._dir = snapshot_dir if snapshot_dir is not None else os.getenv("CMF_GMAIL_SNAPSHOT_DIR")
        self._account = account if account is not None else os.getenv("CMF_GMAIL_ACCOUNT")
        self._cache: Optional[tuple[float, str, _Index]] = None

    def is_configured(self) -> bool:
        return bool(self._dir and self._dir.strip())

    def _index(self) -> tuple[str, _Index]:
        directory = Path(self._dir or "")
        msg_dir = directory / "messages"
        if not msg_dir.is_dir():
            raise ProviderConfigurationError(f"CMF_GMAIL_SNAPSHOT_DIR has no messages/ folder: {directory}")
        stamp = max((p.stat().st_mtime for p in msg_dir.iterdir()), default=0.0)
        if self._cache is None or self._cache[0] != stamp:
            manifest, messages = read_snapshot(directory)
            account = self._account or manifest.get("account") or "unknown"
            self._cache = (stamp, account, _Index(messages))
        return self._cache[1], self._cache[2]

    def query(self, text: str, max_results: int = 10) -> list[KnowledgeResult]:
        terms = _tokens(text)
        if not terms or not self.is_configured():
            return []
        account, index = self._index()
        results = []
        for score, m in index.search(terms, max_results):
            ts = m.timestamp
            results.append(KnowledgeResult(
                provider=self.name,
                document_id=m.id,
                title=m.subject or "(no subject)",
                excerpt=_excerpt(m.body, terms),
                uri=f"https://mail.google.com/mail/u/0/#all/{m.thread_id}",
                source_timestamp=ts,
                retrieval_score=round(score, 4),
                retrieval_method="bm25",
                scope=f"private:gmail:{account}",
                metadata={"from": m.sender, "to": ", ".join(m.to), "thread_id": m.thread_id, "labels": m.labels},
                source_version=m.date,
            ))
        return results
