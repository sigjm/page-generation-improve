"""Read Korean text out of tall product-detail images with macOS Vision (via ocrmac).

Vision handles a 13,000px-tall image poorly, so the image is read in
overlapping tiles and the lines are merged back in reading order.
"""

from pathlib import Path
from typing import Callable

from PIL import Image

# (text, confidence, (x, y, w, h)) with a normalised box whose origin is the bottom-left.
Recognized = list[tuple[str, float, tuple[float, float, float, float]]]
Line = tuple[float, float, str]  # (top px within the tile, left px, text)

ROW_TOLERANCE_PX = 16


def tile_spans(height: int, *, tile: int = 2000, overlap: int = 200) -> list[tuple[int, int]]:
    spans = []
    top = 0
    while True:
        bottom = min(height, top + tile)
        spans.append((top, bottom))
        if bottom >= height:
            return spans
        top = bottom - overlap


def merge_lines(tiles: list[tuple[int, list[Line]]], *, overlap: int) -> list[str]:
    """Place tile lines on the page, drop overlap repeats, join same-row fragments."""
    placed: list[tuple[float, float, str]] = []
    for offset, lines in tiles:
        for top, left, text in lines:
            absolute = offset + top
            if any(text == kept and abs(absolute - kept_top) <= overlap for kept_top, _, kept in placed):
                continue
            placed.append((absolute, left, text))
    placed.sort()
    rows: list[list[tuple[float, float, str]]] = []
    for item in placed:
        if rows and item[0] - rows[-1][0][0] <= ROW_TOLERANCE_PX:
            rows[-1].append(item)
        else:
            rows.append([item])
    return [" ".join(text for _, _, text in sorted(row, key=lambda item: item[1])) for row in rows]


def vision_recognize(tile: Image.Image) -> Recognized:
    from ocrmac import ocrmac  # macOS only; imported lazily so tests and Linux do not need it

    return ocrmac.OCR(tile, language_preference=["ko-KR", "en-US"], recognition_level="accurate").recognize()


def ocr_image(
    path: Path,
    recognize: Callable[[Image.Image], Recognized] = vision_recognize,
    *,
    tile: int = 2000,
    overlap: int = 200,
    min_confidence: float = 0.3,
) -> list[str]:
    image = Image.open(path).convert("RGB")
    tiles: list[tuple[int, list[Line]]] = []
    for top, bottom in tile_spans(image.height, tile=tile, overlap=overlap):
        crop = image.crop((0, top, image.width, bottom))
        lines: list[Line] = []
        for text, confidence, (x, y, w, h) in recognize(crop):
            text = text.strip()
            if confidence < min_confidence or not any(ch.isalnum() for ch in text):
                continue
            lines.append(((1 - y - h) * crop.height, x * crop.width, text))
        tiles.append((top, lines))
    return merge_lines(tiles, overlap=overlap)
