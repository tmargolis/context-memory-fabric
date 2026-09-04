"""Retention policy: decide whether a source event is journaled raw,
redacted, summarized, or excluded, based on a caller-supplied content class.

Milestone 2 scope is deliberately narrow: a real content classifier (what
IS a "medical" or "financial" content class, decided automatically) belongs
to Milestone 3's consolidation pipeline. This module only answers "given a
content_class string an importer already assigned (or 'default' when it
didn't), what happens to the content before it reaches the journal" — the
policy table, plus a minimal, regex-based redactor sufficient to prove the
excluded/redacted/raw distinction actually changes what gets stored.
"""

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Any, Optional


class RetentionClass(StrEnum):
    RAW = "raw"
    REDACTED = "redacted"
    SUMMARIZED = "summarized"
    EXCLUDED = "excluded"


# Conservative default: everything is stored raw unless a caller opts a
# specific content_class into stricter handling. Milestone 3's classifier
# is what will actually assign non-default content classes at scale; until
# then, importers may pass content_class explicitly for known-sensitive
# categories (e.g. the ChatGPT importer already recognizes "medical",
# "financial" style headings during classification).
DEFAULT_POLICY: dict[str, RetentionClass] = {
    "default": RetentionClass.RAW,
}

# Regex-based redaction, intentionally minimal (secret/credential-shaped
# strings only — see IMPLEMENTATION-PLAN.md's MS4a acceptance test "an
# argument containing an API-key-shaped string never reaches the journal",
# which this same redactor will back once capture middleware exists).
_SECRET_PATTERNS = [
    re.compile(r"\bAQ\.[A-Za-z0-9_\-]{20,}\b"),  # Gemini-style API keys (e.g. AQ.Ab8...)
    re.compile(r"\bAIza[A-Za-z0-9_\-]{35}\b"),  # Google API keys
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),  # OpenAI-style secret keys
]

REDACTED_PLACEHOLDER = "[REDACTED]"


@dataclass(frozen=True)
class RetentionPolicy:
    """A content_class -> RetentionClass mapping, with a default fallback."""

    rules: dict[str, RetentionClass] = field(default_factory=lambda: dict(DEFAULT_POLICY))

    def classify(self, content_class: str = "default") -> RetentionClass:
        return self.rules.get(content_class, self.rules.get("default", RetentionClass.RAW))

    def apply(self, content: dict[str, Any], content_class: str = "default") -> Optional[dict[str, Any]]:
        """Apply this policy to `content`.

        Returns:
            The (possibly redacted/summarized) content dict to actually
            journal, or None if retention_class is EXCLUDED — the caller
            (an importer) must treat None as "do not call store.append()
            for this candidate at all," not as an empty-content event.
        """
        retention_class = self.classify(content_class)

        if retention_class == RetentionClass.EXCLUDED:
            return None

        if retention_class == RetentionClass.REDACTED:
            return _redact(content)

        if retention_class == RetentionClass.SUMMARIZED:
            # No summarization model is wired in Milestone 2; treat as
            # redacted (still scrubs secrets) rather than silently falling
            # through to raw, which would defeat the policy's intent.
            return _redact(content)

        return content


def _redact(content: Any) -> Any:
    if isinstance(content, str):
        redacted = content
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(REDACTED_PLACEHOLDER, redacted)
        return redacted
    if isinstance(content, dict):
        return {k: _redact(v) for k, v in content.items()}
    if isinstance(content, list):
        return [_redact(v) for v in content]
    return content
