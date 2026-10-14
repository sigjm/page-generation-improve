from PIL import Image

from detail_page_ai.finetune.ocr import merge_lines, ocr_image, tile_spans


def test_tiles_overlap_and_cover_the_whole_height():
    assert tile_spans(4500, tile=2000, overlap=200) == [(0, 2000), (1800, 3800), (3600, 4500)]
    assert tile_spans(900, tile=2000, overlap=200) == [(0, 900)]


def test_lines_seen_twice_in_an_overlap_are_kept_once_in_reading_order():
    tiles = [
        (0, [(100.0, 50.0, "첫 줄"), (1850.0 - 0, 40.0, "겹친 줄")]),
        (1800, [(50.0, 40.0, "겹친 줄"), (400.0, 300.0, "오른쪽"), (402.0, 20.0, "왼쪽")]),
    ]

    assert merge_lines(tiles, overlap=200) == ["첫 줄", "겹친 줄", "왼쪽 오른쪽"]


def test_ocr_image_reads_tiles_and_drops_low_confidence(tmp_path):
    path = tmp_path / "detail.jpg"
    Image.new("RGB", (800, 3000), "white").save(path)
    seen_heights = []

    def recognize(tile):
        seen_heights.append(tile.height)
        # ocrmac boxes: (x, y, w, h) normalised, origin at the bottom-left
        if len(seen_heights) == 1:
            return [("위쪽 문구", 0.9, (0.1, 0.9, 0.5, 0.02)), ("흐릿함", 0.1, (0.1, 0.5, 0.2, 0.02))]
        return [("아래쪽 문구", 0.8, (0.1, 0.5, 0.5, 0.02))]

    assert ocr_image(path, recognize, tile=2000, overlap=200) == ["위쪽 문구", "아래쪽 문구"]
    assert seen_heights == [2000, 1200]
