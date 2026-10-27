"""Text extraction: native PDF text first, Tesseract OCR for scans/images."""
from dataclasses import dataclass
from pathlib import Path

from ..errors import PermanentError, UnsupportedFileType

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
SUPPORTED_EXT = IMAGE_EXT | {".pdf", ".txt"}
MIN_NATIVE_CHARS = 30


@dataclass
class OcrResult:
    text: str
    method: str  # pdf_text | tesseract | plain_text


def _tesseract(image) -> str:
    try:
        import pytesseract

        return pytesseract.image_to_string(image, lang="eng", config="--psm 6")
    except ImportError as exc:
        raise PermanentError("pytesseract is not installed") from exc
    except pytesseract.TesseractNotFoundError as exc:
        raise PermanentError("Tesseract binary not found (install tesseract-ocr)") from exc


def extract_text(path: Path) -> OcrResult:
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise UnsupportedFileType(f"Unsupported file type: {ext}")
    if ext == ".txt":
        return OcrResult(path.read_text(errors="ignore"), "plain_text")
    if ext in IMAGE_EXT:
        from PIL import Image

        return OcrResult(_tesseract(Image.open(path)), "tesseract")

    import pdfplumber

    try:
        with pdfplumber.open(path) as pdf:
            native = "\n".join((p.extract_text() or "") for p in pdf.pages)
            if len(native.strip()) >= MIN_NATIVE_CHARS:
                return OcrResult(native, "pdf_text")
            # scanned PDF: render pages and OCR them
            pages = [_tesseract(p.to_image(resolution=300).original) for p in pdf.pages]
            return OcrResult("\n".join(pages), "tesseract")
    except (PermanentError, UnsupportedFileType):
        raise
    except Exception as exc:  # corrupt/encrypted PDF
        raise PermanentError(f"Could not read PDF: {exc}") from exc
