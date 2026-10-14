"""Polite crawler for the KCDF craft shop (kcdfshop.kr), for a personal fine-tuning set.

robots.txt is honoured with wildcard support, requests are spaced out, and
every page is parsed into the fields the agent's training data needs.
"""

import html
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

SHOP = "https://www.kcdfshop.kr"
TITLE_SUFFIX = " - 공예정원 온라인숍"
SPEC_FIELDS = ("소재", "규격", "구성", "용도", "인증", "제조")


class RobotsDisallowed(RuntimeError):
    """The path is blocked by robots.txt for our user agent."""


def _groups(robots_txt: str) -> dict[str, list[tuple[str, str]]]:
    groups: dict[str, list[tuple[str, str]]] = {}
    agents: list[str] = []
    last_was_agent = False
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if not last_was_agent:
                agents = []
            agents.append(value.lower())
            groups.setdefault(value.lower(), [])
            last_was_agent = True
        elif key in {"allow", "disallow"}:
            for agent in agents:
                groups[agent].append((key, value))
            last_was_agent = False
        else:
            last_was_agent = False
    return groups


def _rule_matches(rule: str, path: str) -> bool:
    pattern = re.escape(rule).replace(r"\*", ".*")
    if pattern.endswith(r"\$"):
        pattern = pattern[:-2] + "$"
    return re.match(pattern, path) is not None


def robots_allows(robots_txt: str, path: str, user_agent: str = "*") -> bool:
    """Google-style matching: the longest matching rule wins, Allow wins ties."""
    groups = _groups(robots_txt)
    rules = groups.get(user_agent.lower(), groups.get("*", []))
    best: tuple[int, bool] | None = None
    for kind, rule in rules:
        if not rule or not _rule_matches(rule, path):
            continue
        candidate = (len(rule), kind == "allow")
        if best is None or candidate > best:
            best = candidate
    return True if best is None else best[1]


def _default_opener(url: str, user_agent: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class PoliteFetcher:
    def __init__(
        self,
        *,
        base_url: str,
        robots_txt: str,
        user_agent: str,
        min_interval: float,
        opener: Callable[[str, str, float], bytes] = _default_opener,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.robots_txt = robots_txt
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.opener = opener
        self.clock = clock
        self.sleep = sleep
        self.timeout = timeout
        self._last: float | None = None

    def get(self, path_or_url: str) -> bytes:
        url = path_or_url if path_or_url.startswith("http") else self.base_url + path_or_url
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if not robots_allows(self.robots_txt, urllib.parse.unquote(path)):
            raise RobotsDisallowed(path)
        if self._last is not None:
            wait = self.min_interval - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        quoted = urllib.parse.urlunsplit(
            parts._replace(path=urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%"))
        )
        try:
            return self.opener(quoted, self.user_agent, self.timeout)
        finally:
            self._last = self.clock()


@dataclass(frozen=True, slots=True)
class ProductLink:
    product_id: str
    path: str


def parse_list_page(page: str) -> tuple[list[ProductLink], int | None]:
    links: dict[str, ProductLink] = {}
    for path, product_id in re.findall(
        r'href="(/product/[^"?#]*?/(\d+)/category/\d+/display/\d+/)"', page
    ):
        links.setdefault(product_id, ProductLink(product_id, path))
    total = re.search(r'prdCount[^>]*>\s*총\s*([\d,]+)\s*개', page)
    return list(links.values()), int(total.group(1).replace(",", "")) if total else None


def _text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _absolute(src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return SHOP + src
    return src


@dataclass
class ProductPage:
    name: str
    main_image: str | None
    extra_images: list[str] = field(default_factory=list)
    detail_images: list[str] = field(default_factory=list)
    spec: dict[str, str] = field(default_factory=dict)
    notice: str = ""
    brand: str = ""  # the artist, from the page's JSON-LD


def parse_product_page(page: str) -> ProductPage:
    title = re.search(r"<title>([^<]*)</title>", page)
    name = html.unescape(title.group(1)).strip() if title else ""
    name = name.removesuffix(TITLE_SUFFIX).strip()

    og = re.search(r'<meta property="og:image" content="([^"]+)"', page)

    extra: list[str] = []
    block = re.search(r"xans-product-addimage.*?</ul>", page, re.S)
    for src in re.findall(r'<img[^>]+src="([^"]+)"', block.group(0) if block else ""):
        if "/web/product/extra/small/" in src:
            big = _absolute(src.replace("/extra/small/", "/extra/big/"))
            if big not in extra:
                extra.append(big)

    detail_start = page.find('id="prdDetail"')
    detail_end = page.find('id="prdInfo"', detail_start)
    detail = page[detail_start:detail_end if detail_end > 0 else None] if detail_start >= 0 else ""
    detail = re.sub(r"<!--.*?-->", " ", detail, flags=re.S)

    detail_images: list[str] = []
    for src in re.findall(r'<img[^>]+?(?:ec-data-src|src)="([^"]+)"', detail):
        if "/web/upload/" in src and _absolute(src) not in detail_images:
            detail_images.append(_absolute(src))

    spec: dict[str, str] = {}
    spec_at = detail.find("상품상세정보")
    if spec_at >= 0:
        for key, value in re.findall(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", detail[spec_at:], re.S):
            key = _text(key)
            if key in SPEC_FIELDS and key not in spec:
                spec[key] = _text(value)

    # The tab bar also says 유의사항, so read after the last heading before the spec table.
    before_spec = detail[:spec_at] if spec_at >= 0 else detail
    notice_at = before_spec.rfind("유의사항")
    notice = _text(before_spec[notice_at + len("유의사항"):]) if notice_at >= 0 else ""

    brand = re.search(r'"brand"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]*)"', page)
    return ProductPage(
        name=name,
        main_image=og.group(1) if og else None,
        extra_images=extra,
        detail_images=detail_images,
        spec=spec,
        notice=notice,
        brand=brand.group(1).strip() if brand else "",
    )


def crawl_shop(
    out_dir: Path,
    fetcher: PoliteFetcher,
    *,
    categories: dict[str, str],
    image_fetcher: PoliteFetcher | None = None,
    limit: int | None = None,
    max_extra_images: int = 3,
    log: Callable[[str], None] = print,
) -> int:
    """Crawl every category list, then each product once. Resumes from record.json files."""
    image_fetcher = image_fetcher or fetcher
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: dict[str, tuple[str, str]] = {}
    for cate_no, category in categories.items():
        page_no = 1
        while True:
            links, _ = parse_list_page(
                fetcher.get(f"/product/list.html?cate_no={cate_no}&page={page_no}").decode("utf-8", "replace")
            )
            new = [link for link in links if link.product_id not in seen]
            for link in new:
                seen[link.product_id] = (link.path, category)
            if not new:  # past the last page Cafe24 may repeat it instead of returning nothing
                break
            page_no += 1
        log(f"category {category}: {sum(1 for _, c in seen.values() if c == category)} products")

    done = 0
    for product_id, (path, category) in list(seen.items())[:limit]:
        product_dir = out_dir / product_id
        if (product_dir / "record.json").exists():
            done += 1
            continue
        product_dir.mkdir(parents=True, exist_ok=True)
        page_html = fetcher.get(path).decode("utf-8", "replace")
        (product_dir / "page.html").write_text(page_html, encoding="utf-8")
        page = parse_product_page(page_html)
        images: dict[str, list[str]] = {"main": [], "extra": [], "detail": []}
        wanted = [("main", page.main_image)] if page.main_image else []
        wanted += [("extra", url) for url in page.extra_images[:max_extra_images]]
        wanted += [("detail", url) for url in page.detail_images]
        for index, (kind, url) in enumerate(wanted):
            suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower() or ".jpg"
            name = f"{kind}-{index:02d}{suffix}"
            try:
                (product_dir / name).write_bytes(image_fetcher.get(url))
            except (RobotsDisallowed, OSError) as exc:
                log(f"{product_id}: skip {url}: {exc}")
                continue
            images[kind].append(name)
        record = {
            "product_id": product_id,
            "source_url": SHOP + path,
            "category": category,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "rights": "판매 작가 저작물. 개인 학습용 로컬 보관, 재배포 금지",
            "page": asdict(page),
            "images": images,
        }
        (product_dir / "record.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        done += 1
        log(f"[{done}] {product_id} {page.name} ({sum(map(len, images.values()))} images)")
    return done
