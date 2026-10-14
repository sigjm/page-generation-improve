"""Build the agent fine-tuning set from the KCDF craft shop (personal learning only).

    PYTHONPATH=src python scripts/dataset/build_kcdf_agent_finetune.py crawl
    PYTHONPATH=src python scripts/dataset/build_kcdf_agent_finetune.py ocr
    PYTHONPATH=src python scripts/dataset/build_kcdf_agent_finetune.py structure

Every step skips products it already finished, so it can be rerun while the crawl runs.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from pydantic import ValidationError

from detail_page_ai.finetune.crawl import SHOP, PoliteFetcher, ProductPage, crawl_shop
from detail_page_ai.finetune.gold import Structured, clean_ocr_lines, structure_prompt, validate_structured

MIN_OCR_HANGUL = 80

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


def _products(root: Path, needs: str, makes: str):
    for product_dir in sorted((root / "raw").iterdir()):
        if (product_dir / needs).exists() and not (product_dir / makes).exists():
            yield product_dir


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ocr(args: argparse.Namespace) -> int:
    from detail_page_ai.finetune.ocr import ocr_image

    done = 0
    for product_dir in _products(args.root, "record.json", "ocr.json"):
        record = json.loads((product_dir / "record.json").read_text(encoding="utf-8"))
        per_image = {name: ocr_image(product_dir / name) for name in record["images"]["detail"]}
        _write(product_dir / "ocr.json", {"per_image": per_image})
        done += 1
        print(f"[{done}] {product_dir.name}: {sum(map(len, per_image.values()))} lines", flush=True)
    return 0


def ocr_lines(product_dir: Path) -> list[str]:
    per_image = json.loads((product_dir / "ocr.json").read_text(encoding="utf-8"))["per_image"]
    return clean_ocr_lines([line for lines in per_image.values() for line in lines])


def structure(args: argparse.Namespace) -> int:
    from local_detail_page_ai.clients import LocalModelError, MlxServeChatClient

    client = MlxServeChatClient(base_url=args.text_url, model=args.text_model, timeout=600)
    done = 0
    for product_dir in _products(args.root, "ocr.json", "structured.json"):
        record = json.loads((product_dir / "record.json").read_text(encoding="utf-8"))
        page = ProductPage(**record["page"])
        lines = ocr_lines(product_dir)
        out = product_dir / "structured.json"
        if sum("가" <= ch <= "힣" for ch in "".join(lines)) < MIN_OCR_HANGUL:
            _write(out, {"skipped": "ocr text too short"})
            continue
        try:
            raw = Structured.model_validate(client.generate_json(structure_prompt(page, lines)))
        except (LocalModelError, ValidationError) as exc:
            _write(out, {"error": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        kept = validate_structured(raw, lines, page=page)
        _write(out, {"model": args.text_model, "raw": raw.model_dump(), "kept": kept.model_dump()})
        done += 1
        print(f"[{done}] {product_dir.name}: kept {len(kept.features)} features, "
              f"{len(kept.making)} making, hero={'y' if kept.hero_title and kept.hero_description else 'n'}", flush=True)
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
    commands.add_parser("ocr", help="OCR the detail images (macOS Vision).").set_defaults(run=ocr)
    structure_parser = commands.add_parser("structure", help="Sort OCR sentences into roles with a local model.")
    structure_parser.add_argument("--text-url", default="http://127.0.0.1:11234")
    structure_parser.add_argument("--text-model", default="ddalcu/Qwen3.8-27B-MLX-Serve-4bit")
    structure_parser.set_defaults(run=structure)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
