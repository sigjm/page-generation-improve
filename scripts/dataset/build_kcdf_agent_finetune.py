"""Build the agent fine-tuning set from the KCDF craft shop (personal learning only).

    PYTHONPATH=src python scripts/dataset/build_kcdf_agent_finetune.py crawl
"""

import argparse
import sys
import urllib.request
from pathlib import Path

from detail_page_ai.finetune.crawl import SHOP, PoliteFetcher, crawl_shop

ROOT = Path(".local/finetune/kcdf")
USER_AGENT = "Mozilla/5.0 (compatible; personal-research-crawler/0.1; craft fine-tuning study)"
# Shop menu categories (by use); 578 is 전체상품 and catches whatever the others miss.
CATEGORIES = {
    "587": "인테리어",
    "584": "주방용품",
    "579": "문구/사무",
    "590": "생활용품",
    "582": "패션/잡화",
    "791": "디자인",
    "792": "목공",
    "578": "기타",
}


def crawl(args: argparse.Namespace) -> int:
    request = urllib.request.Request(f"{SHOP}/robots.txt", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        robots_txt = response.read().decode("utf-8", "replace")
    (args.root / "robots.txt").parent.mkdir(parents=True, exist_ok=True)
    (args.root / "robots.txt").write_text(robots_txt, encoding="utf-8")

    def fetcher(interval: float) -> PoliteFetcher:
        return PoliteFetcher(
            base_url=SHOP, robots_txt=robots_txt, user_agent=USER_AGENT, min_interval=interval
        )

    done = crawl_shop(
        args.root / "raw",
        fetcher(args.page_interval),
        categories=CATEGORIES,
        image_fetcher=fetcher(args.image_interval),
        limit=args.limit,
        log=lambda line: print(line, flush=True),
    )
    print(f"crawled {done} products into {args.root / 'raw'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    crawl_parser = commands.add_parser("crawl", help="Download product pages and images.")
    crawl_parser.add_argument("--limit", type=int)
    crawl_parser.add_argument("--page-interval", type=float, default=2.0)
    crawl_parser.add_argument("--image-interval", type=float, default=1.0)
    crawl_parser.set_defaults(run=crawl)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
