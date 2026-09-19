"""Corpus models, configuration validation, and path utilities for Context Memory Fabric.

Defines the normalized data representation for discovered durable knowledge assets
and search results, along with strict environment-based path validation.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import mimetypes
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv


class ExtractionStatus(StrEnum):
    """Normalized content extraction status for corpus assets."""

    EXTRACTED = "extracted"
    NEEDS_OCR = "needs_ocr"
    NEEDS_IMAGE_UNDERSTANDING = "needs_image_understanding"
    NEEDS_TRANSCRIPTION = "needs_transcription"
    UNSUPPORTED = "unsupported_or_future_extractor"
    ERROR = "extraction_error"


class MatchBasis(StrEnum):
    """Signal indicating what part of the asset matched the search query."""

    CONTENT = "content"
    FILENAME = "filename"
    PATH = "path"
    METADATA = "metadata"


# Directories that contain non-knowledge internal, VCS, cache, temporary files, or excluded utility folders
IGNORED_DIR_NAMES = {
    ".git",
    ".obsidian",
    ".trash",
    ".stfolder",
    ".claude",
    ".tmp.drivedownload",
    ".tmp.driveupload",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "env",
    ".idea",
    ".vscode",
    "_lint_reports",
    "_profile_reports",
    "templates",
    "memory",
}

# Exact filenames or patterns that contain OS or sync metadata
IGNORED_EXACT_FILENAMES = {
    ".DS_Store",
    "Thumbs.db",
    ".gitattributes",
    ".gitignore",
    ".stignore",
}

# File extensions for temporary / editor swap files
IGNORED_EXTENSIONS = {
    ".tmp",
    ".swp",
    ".swo",
    "~",
}

# Custom extension to MIME type mapping overrides
EXTENSION_MIME_MAP = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".csv": "text/csv",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".html": "text/html",
    ".sh": "text/x-shellscript",
    ".py": "text/x-python",
    ".base": "text/plain",
}


def guess_media_type(extension: str) -> str:
    """Return a best-effort MIME/media type for a given file extension."""
    ext = extension.lower()
    if ext in EXTENSION_MIME_MAP:
        return EXTENSION_MIME_MAP[ext]
    mime, _ = mimetypes.guess_type(f"file{ext}")
    return mime or "application/octet-stream"


def get_corpus_root(env_file: Optional[Path] = None) -> Path:
    """Retrieve and validate the LLM_WIKI_PATH from the project root .env.

    Raises:
        RuntimeError: If LLM_WIKI_PATH is unset, empty, non-existent, not a
            directory, or unreadable.
    """
    if env_file is not None:
        load_dotenv(dotenv_path=env_file, override=True)
    else:
        load_dotenv()

    raw_path = os.getenv("LLM_WIKI_PATH")
    if not raw_path or not raw_path.strip():
        raise RuntimeError(
            "LLM_WIKI_PATH is not set in environment or .env file. "
            "Please configure LLM_WIKI_PATH=/path/to/LLM_Wiki in your project-root .env."
        )

    resolved = Path(raw_path.strip()).expanduser().resolve()

    if not resolved.exists():
        raise RuntimeError(
            f"LLM_WIKI_PATH directory does not exist: '{raw_path}' (resolved: '{resolved}')"
        )

    if not resolved.is_dir():
        raise RuntimeError(
            f"LLM_WIKI_PATH is not a directory: '{raw_path}' (resolved: '{resolved}')"
        )

    if not os.access(resolved, os.R_OK):
        raise RuntimeError(
            f"LLM_WIKI_PATH is not readable: '{raw_path}' (resolved: '{resolved}')"
        )

    return resolved


def is_excluded_path(rel_path: Path) -> bool:
    """Check whether a relative file path should be excluded from corpus scanning."""
    parts = rel_path.parts

    # Check any directory component
    for part in parts[:-1]:
        if part in IGNORED_DIR_NAMES or part.startswith("."):
            return True

    # Check filename
    filename = rel_path.name
    if filename.startswith(".") or filename in IGNORED_EXACT_FILENAMES:
        return True

    for ext in IGNORED_EXTENSIONS:
        if filename.endswith(ext):
            return True

    return False


@dataclass
class CorpusAsset:
    """Normalized record for a discovered asset in the durable knowledge corpus."""

    source: str = "durable_knowledge"
    relative_path: str = ""
    filename: str = ""
    top_level_area: str = "ROOT"
    extension: str = ""
    media_type: str = "application/octet-stream"
    size_bytes: int = 0
    modified_time: str = ""
    extraction_status: str = ExtractionStatus.UNSUPPORTED.value
    extractor: str = "none"
    extracted_text: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    """Normalized search result returned by the durable knowledge retrieval layer."""

    source: str = "durable_knowledge"
    relative_path: str = ""
    filename: str = ""
    top_level_area: str = "ROOT"
    media_type: str = "application/octet-stream"
    extractor: str = "none"
    extraction_status: str = ExtractionStatus.UNSUPPORTED.value
    matched_snippet: Optional[str] = None
    match_basis: str = MatchBasis.CONTENT.value
    query: str = ""
    relevance_score: float = 0.0
