"""Modular extractors for heterogeneous corpus assets.

Provides format-specific text extraction, metadata extraction, and classification
for Markdown, plain text, PDFs, structured text, images, audio, and unsupported formats.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any, Optional

from pypdf import PdfReader

from server.providers.wiki.corpus import CorpusAsset, ExtractionStatus

logger = logging.getLogger(__name__)

# Suppress harmless pypdf syntax warnings during corpus scan
logging.getLogger("pypdf").setLevel(logging.ERROR)

# C0 control characters other than tab / newline / carriage-return. pypdf
# occasionally emits these (a NUL byte in the 1400 State Pkwy inspection PDF
# broke a downstream subprocess); strip them so search_wiki / get_context
# never hand a control char to an MCP client.
_CTRL_CHARS = {c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)}
_CTRL_CHARS[0x7F] = None


def _clean_text(text: Optional[str]) -> Optional[str]:
    return text.translate(_CTRL_CHARS) if isinstance(text, str) else text


@dataclass
class ExtractionResult:
    """Result of content extraction from a corpus asset."""

    extracted_text: Optional[str] = None
    extraction_status: str = ExtractionStatus.UNSUPPORTED.value
    extractor_name: str = "none"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Every extractor's text flows through here; sanitize once.
        self.extracted_text = _clean_text(self.extracted_text)


class BaseExtractor(ABC):
    """Abstract base class for asset extractors."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the extractor."""
        ...

    @abstractmethod
    def extract(self, file_path: Path, asset: CorpusAsset) -> ExtractionResult:
        """Extract content or metadata from the given asset."""
        ...


class TextExtractor(BaseExtractor):
    """Extracts plain text and Markdown content."""

    @property
    def name(self) -> str:
        return "text_extractor"

    def extract(self, file_path: Path, asset: CorpusAsset) -> ExtractionResult:
        try:
            # Try UTF-8 first, fallback with errors='replace' to avoid decode crashes
            content = file_path.read_text(encoding="utf-8", errors="replace")
            return ExtractionResult(
                extracted_text=content,
                extraction_status=ExtractionStatus.EXTRACTED.value,
                extractor_name=self.name,
                metadata={"character_count": len(content), "line_count": content.count("\n") + 1},
            )
        except Exception as e:
            logger.warning(f"Error reading text file {file_path}: {e}")
            return ExtractionResult(
                extracted_text=None,
                extraction_status=ExtractionStatus.ERROR.value,
                extractor_name=self.name,
                metadata={"error": str(e)},
            )


class PdfExtractor(BaseExtractor):
    """Extracts embedded selectable text from PDF documents using pypdf.

    If the PDF contains no extractable text (e.g. scanned image), records
    extraction_status = needs_ocr.
    """

    @property
    def name(self) -> str:
        return "pdf_extractor"

    def extract(self, file_path: Path, asset: CorpusAsset) -> ExtractionResult:
        try:
            reader = PdfReader(str(file_path))
            num_pages = len(reader.pages)
            pages_text = []

            for idx, page in enumerate(reader.pages):
                try:
                    text = page.extract_text()
                    if text and text.strip():
                        pages_text.append(text.strip())
                except Exception as page_err:
                    logger.debug(f"Failed extracting text from page {idx} in {file_path}: {page_err}")

            full_text = "\n\n".join(pages_text).strip()

            if full_text:
                return ExtractionResult(
                    extracted_text=full_text,
                    extraction_status=ExtractionStatus.EXTRACTED.value,
                    extractor_name=self.name,
                    metadata={"page_count": num_pages, "extracted_page_count": len(pages_text)},
                )
            else:
                return ExtractionResult(
                    extracted_text=None,
                    extraction_status=ExtractionStatus.NEEDS_OCR.value,
                    extractor_name=self.name,
                    metadata={"page_count": num_pages},
                )
        except Exception as e:
            logger.warning(f"Error extracting PDF text from {file_path}: {e}")
            return ExtractionResult(
                extracted_text=None,
                extraction_status=ExtractionStatus.ERROR.value,
                extractor_name=self.name,
                metadata={"error": str(e)},
            )


class StructuredTextExtractor(BaseExtractor):
    """Extracts textual representation from JSON, CSV, and YAML files."""

    @property
    def name(self) -> str:
        return "structured_text_extractor"

    def extract(self, file_path: Path, asset: CorpusAsset) -> ExtractionResult:
        try:
            raw_content = file_path.read_text(encoding="utf-8", errors="replace")
            meta = {}

            if asset.extension.lower() == ".json":
                try:
                    parsed = json.loads(raw_content)
                    if isinstance(parsed, dict):
                        meta["json_keys"] = list(parsed.keys())[:20]
                    elif isinstance(parsed, list):
                        meta["json_item_count"] = len(parsed)
                except Exception:
                    pass

            return ExtractionResult(
                extracted_text=raw_content,
                extraction_status=ExtractionStatus.EXTRACTED.value,
                extractor_name=self.name,
                metadata=meta,
            )
        except Exception as e:
            logger.warning(f"Error reading structured text from {file_path}: {e}")
            return ExtractionResult(
                extracted_text=None,
                extraction_status=ExtractionStatus.ERROR.value,
                extractor_name=self.name,
                metadata={"error": str(e)},
            )


class ImageMetadataExtractor(BaseExtractor):
    """Extracts metadata for images and marks them for future visual understanding."""

    @property
    def name(self) -> str:
        return "image_metadata_extractor"

    def extract(self, file_path: Path, asset: CorpusAsset) -> ExtractionResult:
        return ExtractionResult(
            extracted_text=None,
            extraction_status=ExtractionStatus.NEEDS_IMAGE_UNDERSTANDING.value,
            extractor_name=self.name,
            metadata={"format": asset.extension.lstrip(".").lower()},
        )


class AudioMetadataExtractor(BaseExtractor):
    """Extracts metadata for audio/video and marks them for future transcription."""

    @property
    def name(self) -> str:
        return "audio_metadata_extractor"

    def extract(self, file_path: Path, asset: CorpusAsset) -> ExtractionResult:
        return ExtractionResult(
            extracted_text=None,
            extraction_status=ExtractionStatus.NEEDS_TRANSCRIPTION.value,
            extractor_name=self.name,
            metadata={"format": asset.extension.lstrip(".").lower()},
        )


class FallbackExtractor(BaseExtractor):
    """Default extractor for unsupported or future document/binary formats."""

    @property
    def name(self) -> str:
        return "fallback_extractor"

    def extract(self, file_path: Path, asset: CorpusAsset) -> ExtractionResult:
        return ExtractionResult(
            extracted_text=None,
            extraction_status=ExtractionStatus.UNSUPPORTED.value,
            extractor_name=self.name,
            metadata={"extension": asset.extension},
        )


class ExtractorRegistry:
    """Registry mapping file extensions to specialized extractors."""

    def __init__(self) -> None:
        self._extractors: dict[str, BaseExtractor] = {}
        self._fallback = FallbackExtractor()
        self._register_defaults()

    def _register_defaults(self) -> None:
        text_ext = TextExtractor()
        for ext in [
            ".md",
            ".markdown",
            ".txt",
            ".html",
            ".sh",
            ".py",
            ".base",
            ".css",
            ".js",
            ".ts",
        ]:
            self._extractors[ext] = text_ext

        pdf_ext = PdfExtractor()
        self._extractors[".pdf"] = pdf_ext

        struct_ext = StructuredTextExtractor()
        for ext in [".json", ".csv", ".yaml", ".yml"]:
            self._extractors[ext] = struct_ext

        img_ext = ImageMetadataExtractor()
        for ext in [".png", ".jpg", ".jpeg", ".tiff", ".gif", ".webp", ".bmp", ".svg"]:
            self._extractors[ext] = img_ext

        audio_ext = AudioMetadataExtractor()
        for ext in [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".mkv"]:
            self._extractors[ext] = audio_ext

    def register(self, extension: str, extractor: BaseExtractor) -> None:
        """Register a custom extractor for a given extension."""
        self._extractors[extension.lower()] = extractor

    def get_extractor(self, extension: str) -> BaseExtractor:
        """Get the extractor matching the extension, or fallback."""
        return self._extractors.get(extension.lower(), self._fallback)


# Global default extractor registry
DEFAULT_REGISTRY = ExtractorRegistry()
