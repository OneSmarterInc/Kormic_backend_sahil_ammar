"""Safe path handling for retained institute roster source files."""
from pathlib import Path

from django.conf import settings


def resolve_source_file_path(stored_path: str) -> Path:
    """Resolve a stored relative path inside MEDIA_ROOT.

    Absolute paths written by older releases are accepted only when they
    still resolve inside MEDIA_ROOT. Any path that escapes MEDIA_ROOT is
    rejected so a corrupted database value cannot expose or delete an
    arbitrary server file.
    """
    media_root = Path(settings.MEDIA_ROOT).resolve()
    path = Path(stored_path)
    candidate = path.resolve() if path.is_absolute() else (media_root / path).resolve()
    try:
        candidate.relative_to(media_root)
    except ValueError as exc:
        raise ValueError("source file path is outside MEDIA_ROOT") from exc
    return candidate
