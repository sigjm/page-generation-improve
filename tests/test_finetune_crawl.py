from pathlib import Path

import pytest

from detail_page_ai.finetune.crawl import (
    PoliteFetcher,
    RobotsDisallowed,
    parse_list_page,
    parse_product_page,
    robots_allows,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kcdf"

KCDF_ROBOTS = """User-agent: *
Disallow: /admin
Disallow: /api
Disallow: /member/
Disallow: /*board*page=
Disallow: /*page_4=

User-agent: bingbot
Crawl-delay: 10
Disallow: /
"""


@pytest.mark.parametrize(
    "path, allowed",
    [
        ("/product/list.html?cate_no=578&page=2", True),
        ("/product/합성-찻잔/101/category/578/display/1/", True),
        ("/web/upload/NNEditor/20261008/detail-1.jpg", True),
        ("/admin/login", False),
        ("/member/join.html", False),
        ("/board/free/list.html?board_no=1&page=2", False),
        ("/product/list.html?page_4=3", False),
    ],
)
def test_robots_rules_with_wildcards(path, allowed):
    assert robots_allows(KCDF_ROBOTS, path) is allowed


def test_robots_uses_the_specific_agent_group_and_longest_match():
    robots = "User-agent: *\nDisallow: /a\nAllow: /a/b$\n\nUser-agent: mybot\nDisallow: /\n"
    assert robots_allows(robots, "/a/b") is True
    assert robots_allows(robots, "/a/b/c") is False
    assert robots_allows(robots, "/x", user_agent="mybot") is False


def test_list_page_yields_unique_product_ids_and_total():
    links, total = parse_list_page((FIXTURES / "list.html").read_text(encoding="utf-8"))

    assert total == 3
    assert [link.product_id for link in links] == ["101", "102"]
    assert links[0].path == "/product/합성-찻잔/101/category/578/display/1/"


def test_product_page_fields():
    page = parse_product_page((FIXTURES / "product.html").read_text(encoding="utf-8"))

    assert page.name == "합성 찻잔"
    assert page.main_image == "https://www.kcdfshop.kr/web/product/big/202610/main.jpg"
    assert page.extra_images == [
        "https://www.kcdfshop.kr/web/product/extra/big/202610/extra1.jpg",
        "https://www.kcdfshop.kr/web/product/extra/big/202610/extra2.jpg",
    ]
    assert page.detail_images == [
        "https://www.kcdfshop.kr/web/upload/NNEditor/20261008/detail-1.jpg",
        "https://www.kcdfshop.kr/web/upload/NNEditor/20261008/detail-2.jpg",
        # Products migrated from the old shop keep their detail images under the godomall path.
        "https://www.kcdfshop.kr/web/kcdfonline1_godomall_com/data/editor/goods/210622/old-detail.jpg",
    ]
    assert page.spec == {
        "소재": "도자",
        "규격": "Ø 80 x H113mm",
        "구성": "잔1P /세트시 2P",
        "용도": "물잔 /주방용품",
        "인증": "해당 없음",
        "제조": "대한민국",
    }
    assert page.notice == "전자레인지 사용은 피해 주세요. 모든 제품은 수작업이라 조금씩 다릅니다."


class FakeOpener:
    def __init__(self):
        self.urls = []

    def __call__(self, url, user_agent, timeout):
        self.urls.append(url)
        return b"ok"


def test_fetcher_refuses_disallowed_paths_and_spaces_requests():
    now = [100.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    opener = FakeOpener()
    fetcher = PoliteFetcher(
        base_url="https://www.kcdfshop.kr",
        robots_txt=KCDF_ROBOTS,
        user_agent="personal-research-crawler/0.1",
        min_interval=2.0,
        opener=opener,
        clock=lambda: now[0],
        sleep=sleep,
    )

    fetcher.get("/product/list.html?cate_no=578&page=1")
    fetcher.get("/product/list.html?cate_no=578&page=2")
    with pytest.raises(RobotsDisallowed):
        fetcher.get("/member/join.html")

    assert opener.urls == [
        "https://www.kcdfshop.kr/product/list.html?cate_no=578&page=1",
        "https://www.kcdfshop.kr/product/list.html?cate_no=578&page=2",
    ]
    assert slept == [2.0]


def test_fetcher_percent_encodes_korean_paths():
    opener = FakeOpener()
    fetcher = PoliteFetcher(
        base_url="https://www.kcdfshop.kr",
        robots_txt=KCDF_ROBOTS,
        user_agent="ua",
        min_interval=0,
        opener=opener,
    )

    fetcher.get("/product/합성-찻잔/101/category/578/display/1/")

    assert opener.urls == [
        "https://www.kcdfshop.kr/product/%ED%95%A9%EC%84%B1-%EC%B0%BB%EC%9E%94/101/category/578/display/1/"
    ]


def test_crawl_stops_when_a_list_page_repeats_and_resumes(tmp_path):
    from detail_page_ai.finetune.crawl import crawl_shop

    list_html = (FIXTURES / "list.html").read_bytes()
    product_html = (FIXTURES / "product.html").read_bytes()
    requested = []

    def opener(url, user_agent, timeout):
        requested.append(url)
        if "list.html" in url:
            return list_html  # Cafe24 can repeat the last page instead of ending
        if "/product/" in url and "/category/" in url:
            return product_html
        return b"image-bytes"

    def fetcher():
        return PoliteFetcher(
            base_url="https://www.kcdfshop.kr",
            robots_txt=KCDF_ROBOTS,
            user_agent="ua",
            min_interval=0,
            opener=opener,
        )

    done = crawl_shop(tmp_path, fetcher(), categories={"578": "전체"}, log=lambda _: None)

    assert done == 2
    assert sum("list.html" in url for url in requested) == 2
    record = (tmp_path / "101" / "record.json").read_text(encoding="utf-8")
    assert '"main": [' in record and "main-00.jpg" in record
    assert sorted(p.name for p in (tmp_path / "101").iterdir()) == [
        "detail-03.jpg",
        "detail-04.jpg",
        "detail-05.jpg",
        "extra-01.jpg",
        "extra-02.jpg",
        "main-00.jpg",
        "page.html",
        "record.json",
    ]

    requested.clear()
    assert crawl_shop(tmp_path, fetcher(), categories={"578": "전체"}, log=lambda _: None) == 2
    assert not any("/category/578/display" in url for url in requested)


def test_product_page_reads_the_artist_from_json_ld():
    page = parse_product_page((FIXTURES / "product.html").read_text(encoding="utf-8"))

    assert page.brand == "합성 작가"


def test_crawl_repairs_products_missing_detail_images(tmp_path):
    import json

    from detail_page_ai.finetune.crawl import crawl_shop

    list_html = (FIXTURES / "list.html").read_bytes()
    product_html = (FIXTURES / "product.html").read_bytes()

    def opener(url, user_agent, timeout):
        if "list.html" in url:
            return list_html
        if "/product/" in url and "/category/" in url:
            return product_html
        return b"image-bytes"

    def fetcher():
        return PoliteFetcher(base_url="https://www.kcdfshop.kr", robots_txt=KCDF_ROBOTS, user_agent="ua", min_interval=0, opener=opener)

    crawl_shop(tmp_path, fetcher(), categories={"578": "전체"}, log=lambda _: None)
    product = tmp_path / "101"
    record = json.loads((product / "record.json").read_text(encoding="utf-8"))
    # As written by the old parser: no godomall detail image.
    record["page"]["detail_images"] = record["page"]["detail_images"][:2]
    record["images"]["detail"] = ["detail-03.jpg", "detail-04.jpg"]
    (product / "record.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    (product / "detail-05.jpg").unlink()
    (product / "ocr.json").write_text("{}", encoding="utf-8")
    (product / "structured.json").write_text("{}", encoding="utf-8")

    crawl_shop(tmp_path, fetcher(), categories={"578": "전체"}, log=lambda _: None)

    repaired = json.loads((product / "record.json").read_text(encoding="utf-8"))
    assert repaired["images"]["detail"] == ["detail-03.jpg", "detail-04.jpg", "detail-05.jpg"]
    assert len(repaired["page"]["detail_images"]) == 3
    assert (product / "detail-05.jpg").read_bytes() == b"image-bytes"
    assert not (product / "ocr.json").exists() and not (product / "structured.json").exists()
