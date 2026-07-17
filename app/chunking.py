from __future__ import annotations


def split_text(text: str, max_chars: int) -> list[str]:
    """Split text without dropping characters, preferring natural boundaries."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if not text:
        return []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[: max_chars + 1]
        split_at = _best_boundary(window, max_chars)
        if split_at <= 0:
            split_at = max_chars
        chunk = remaining[:split_at]
        if not chunk:
            split_at = max_chars
            chunk = remaining[:split_at]
        chunks.append(chunk)
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _best_boundary(window: str, max_chars: int) -> int:
    minimum = max(1, max_chars // 2)
    for marker in ("\n\n", "\n", ". ", "! ", "? ", " "):
        position = window.rfind(marker, minimum, max_chars + 1)
        if position >= 0:
            return min(max_chars, position + len(marker))
    return max_chars
