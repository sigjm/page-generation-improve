"""Build the agent fine-tuning set from the KCDF craft shop (personal learning only).

    PYTHONPATH=src python scripts/dataset/build_kcdf_agent_finetune.py crawl
    PYTHONPATH=src python scripts/dataset/build_kcdf_agent_finetune.py ocr
    PYTHONPATH=src python scripts/dataset/build_kcdf_agent_finetune.py structure
    PYTHONPATH=src python scripts/dataset/build_kcdf_agent_finetune.py build

Every step skips products it already finished, so it can be rerun while the crawl runs.
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from pydantic import ValidationError

from detail_page_ai.finetune.crawl import SHOP, PoliteFetcher, ProductPage, crawl_shop, parse_product_page
from detail_page_ai.finetune.gold import (
    Structured,
    clean_ocr_lines,
    join_sentences,
    structure_prompt,
    validate_structured,
)

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


def _write(path: Path, data: dict | list) -> None:
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


def _page(product_dir: Path) -> ProductPage:
    """Parse the saved HTML again, so parser fixes apply without re-crawling."""
    return parse_product_page((product_dir / "page.html").read_text(encoding="utf-8"))


def ocr_lines(product_dir: Path) -> list[str]:
    per_image = json.loads((product_dir / "ocr.json").read_text(encoding="utf-8"))["per_image"]
    return join_sentences(clean_ocr_lines([line for lines in per_image.values() for line in lines]))


def _teacher(args: argparse.Namespace):
    """The labeling model: Codex by default, or a local MLX Serve model."""
    if args.teacher == "codex":
        from detail_page_ai.finetune.codex import CodexJsonClient

        return CodexJsonClient(reasoning=args.codex_reasoning, service_tier=args.codex_tier)
    from local_detail_page_ai.clients import MlxServeChatClient

    return MlxServeChatClient(base_url=args.text_url, model=args.text_model, timeout=600)


def structure(args: argparse.Namespace) -> int:
    from concurrent.futures import ThreadPoolExecutor

    from detail_page_ai.finetune.codex import CodexError
    from local_detail_page_ai.clients import LocalModelError

    client = _teacher(args)
    teacher = "codex" if args.teacher == "codex" else args.text_model

    def one(product_dir: Path) -> str:
        page = _page(product_dir)
        lines = ocr_lines(product_dir)
        out = product_dir / "structured.json"
        if sum("가" <= ch <= "힣" for ch in "".join(lines)) < MIN_OCR_HANGUL:
            _write(out, {"skipped": "ocr text too short"})
            return "skipped"
        try:
            raw = Structured.model_validate(client.generate_json(structure_prompt(page, lines)))
        except ValidationError as exc:
            _write(out, {"error": f"{type(exc).__name__}: {exc}"[:500]})
            return "invalid"
        except (LocalModelError, CodexError) as exc:
            # Server down, rate limit or a non-JSON reply: record nothing, so the next run retries.
            print(f"{product_dir.name}: teacher error, will retry: {exc}"[:300], flush=True)
            return "retry"
        kept = validate_structured(raw, lines, page=page)
        _write(out, {"model": teacher, "raw": raw.model_dump(), "kept": kept.model_dump()})
        print(f"{product_dir.name}: kept {len(kept.features)} features, {len(kept.making)} making, "
              f"hero={'y' if kept.hero_title and kept.hero_description else 'n'}", flush=True)
        return "ok"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, list(_products(args.root, "ocr.json", "structured.json"))))
    print({result: results.count(result) for result in set(results)}, flush=True)
    return 1 if "retry" in results else 0


def _resized_jpeg(path: Path, longest: int = 1280) -> bytes:
    from io import BytesIO

    from PIL import Image

    image = Image.open(path).convert("RGB")
    image.thumbnail((longest, longest))
    output = BytesIO()
    image.save(output, format="JPEG", quality=90)
    return output.getvalue()


def _variant(product_id: str, salt: str) -> int:
    import hashlib

    return int(hashlib.sha256(f"{product_id}:{salt}".encode()).hexdigest(), 16) % 10


def build(args: argparse.Namespace) -> int:
    import hashlib
    from collections import Counter

    from detail_page_ai.finetune.conversations import (
        draft_conversation,
        edit_conversation,
        pick_edit_templates,
        render_conversation,
        runtime_draft,
    )
    from detail_page_ai.finetune.export import analysis_row, group_products, judged_ok, split_for
    from detail_page_ai.finetune.gold import (
        available_blocks,
        build_gold_profile,
        build_user_hints,
        check_gold,
        choose_archetype,
        hint_lines,
        page_as_given,
    )
    from detail_page_ai.layout_archetypes import select_layout_archetypes

    raw = args.root / "raw"
    out = args.root / "dataset"
    (out / "images").mkdir(parents=True, exist_ok=True)

    products = {}
    for product_dir in sorted(raw.iterdir()):
        if not (product_dir / "structured.json").exists():
            continue
        structured = json.loads((product_dir / "structured.json").read_text(encoding="utf-8"))
        if "raw" not in structured:
            continue
        record = json.loads((product_dir / "record.json").read_text(encoding="utf-8"))
        per_image = json.loads((product_dir / "ocr.json").read_text(encoding="utf-8"))["per_image"]
        products[product_dir.name] = (product_dir, record, structured, per_image)

    groups = group_products({
        product_id: {
            hashlib.sha256((product_dir / name).read_bytes()).hexdigest(): clean_ocr_lines(lines)
            for name, lines in per_image.items()
        }
        for product_id, (product_dir, _, _, per_image) in products.items()
    }, brands={product_id: _page(product_dir).brand for product_id, (product_dir, *_) in products.items()})

    judge = None if args.no_judge else _teacher(args)
    teacher = "codex" if args.teacher == "codex" else args.text_model
    # Match the final agent: Qwen-Image on, so analysis prompts and photo sets are the runtime ones.
    image_generation = bool(args.image_model)

    photo_generator = renderer = None
    if not args.skip_render:
        from detail_page_ai.html_renderer import HtmlDetailPageRenderer
        from detail_page_ai.source_photos import SourcePreservingProductPhotoGenerator

        generators = {}
        if image_generation:
            from local_detail_page_ai.clients import MlxServeImageClient
            from local_detail_page_ai.runner import (
                MlxServeBackgroundGenerator,
                MlxServeDetailViewGenerator,
                MlxServeUsageSceneGenerator,
            )

            image_client = MlxServeImageClient(base_url=args.image_url, model=args.image_model, timeout=900, steps=args.image_steps)
            generators = {
                "background_generator": MlxServeBackgroundGenerator(image_client),
                "usage_scene_generator": MlxServeUsageSceneGenerator(image_client),
                "detail_view_generator": MlxServeDetailViewGenerator(image_client),
            }
        # Same shots and cap as the service defaults (PRODUCT_PHOTO_SHOTS, MAX_GENERATED_PHOTOS).
        photo_generator = SourcePreservingProductPhotoGenerator(
            **generators, include_scale=False, photo_roles=("hero", "packshot", "detail", "lifestyle"), max_generated_photos=5,
        )
        renderer = HtmlDetailPageRenderer()

    files = {name: {split: (out / name / f"{split}.jsonl") for split in ("train", "val")} for name in ("vlm", "agent")}
    for by_split in files.values():
        for path in by_split.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
    meta_path = out / "meta.jsonl"
    meta_path.write_text("", encoding="utf-8")
    stats: Counter = Counter()
    per_group_analysis: Counter = Counter()
    rendered_groups: set[str] = set()

    def write(kind: str, file: str, product_id: str, category: str, row: dict) -> None:
        group = groups[product_id]
        split = split_for(group)
        row_id = f"kcdf-{product_id}-{kind}-{stats[(product_id, kind)]}"
        stats[(product_id, kind)] += 1
        with files[file][split].open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": row_id, **row}, ensure_ascii=False) + "\n")
        with meta_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "id": row_id, "kind": kind, "file": f"{file}/{split}.jsonl", "product_id": product_id,
                "group": group, "category": category, "split": split, "source_url": products[product_id][1]["source_url"],
                "artist": _page(products[product_id][0]).brand,
            }, ensure_ascii=False) + "\n")
        stats[kind] += 1
        stats[f"{kind}:{split}"] += 1

    # 1) Gold profiles, in product order (rules only, fast). Which products carry the analysis
    #    rows and the one render per group is decided here, so it does not depend on timing.
    jobs = []
    for product_id, (product_dir, record, structured, per_image) in products.items():
        page = _page(product_dir)
        category = record["category"]
        if not record["images"]["main"] or not (page.spec.get("소재") or page.spec.get("용도")):
            stats["skip:no-main-or-spec"] += 1
            continue
        hints = build_user_hints(page, ocr_lines(product_dir))
        kept = validate_structured(Structured.model_validate(structured["raw"]), hint_lines(hints), page=page)
        image = _resized_jpeg(product_dir / record["images"]["main"][0])
        image_name = f"images/{product_id}.jpg"
        (out / image_name).write_bytes(image)
        archetypes = select_layout_archetypes(
            hashlib.sha256(image).hexdigest(), hints, count=4, image_generation_enabled=image_generation,
        )
        archetype = choose_archetype(archetypes, available_blocks(page_as_given(page, hints), kept))
        profile = build_gold_profile(page, category, kept, archetype, hints)
        if profile is None:
            stats["skip:too-little-grounded-copy"] += 1
            continue
        try:
            check_gold(profile, hints)
        except Exception as exc:  # noqa: BLE001 - recorded and skipped, never shipped
            stats["skip:runtime-check"] += 1
            print(f"{product_id}: gold failed runtime checks: {exc}", flush=True)
            continue
        group = groups[product_id]
        analysis = None
        if per_group_analysis[group] < args.max_per_group:
            per_group_analysis[group] += 1
            analysis = analysis_row(image_name, image, profile, hints, image_generation_enabled=image_generation)
        render = renderer is not None and group not in rendered_groups
        rendered_groups.add(group)
        extras = tuple((_resized_jpeg(product_dir / name), "image/jpeg") for name in record["images"].get("extra", [])[:3])
        jobs.append((product_id, category, hints, profile, image, image_name, extras, analysis, render))

    # 2) Agent conversations in parallel: the teacher's image answers and Qwen-Image photos are slow.
    cache = out / "cache"  # one file per finished product, so an interrupted build resumes
    cache.mkdir(exist_ok=True)

    def conversations(job) -> list[tuple[str, str, dict]]:
        product_id, analysis, render = job[0], job[7], job[8]
        head = [("analysis", "vlm", analysis)] if analysis else []
        cached = cache / f"{product_id}-{'render' if render else 'norender'}.json"
        if cached.exists():
            return head + [tuple(row) for row in json.loads(cached.read_text(encoding="utf-8"))]
        try:
            rows = fresh_conversations(job)
        except Exception as exc:  # noqa: BLE001 - one product must not stop the build; not cached, so retried
            print(f"{product_id}: conversation failed, will retry on the next build: {exc}"[:300], flush=True)
            return head + [("skip:conversation-error", "", {})]
        _write(cached, rows)
        return head + rows

    def fresh_conversations(job) -> list[tuple[str, str, dict]]:
        product_id, category, hints, profile, image, image_name, extras, analysis, render = job
        rows = []
        known = "\n".join(hint_lines(hints))
        judged_images = 0

        def judged_rows(requests: list[dict]) -> list[tuple[str, str, dict]]:
            nonlocal judged_images
            result = []
            for request in requests:
                suffix = "png" if request["mime_type"] == "image/png" else "jpg"
                name = f"images/{product_id}-judge-{judged_images}.{suffix}"
                judged_images += 1
                (out / name).write_bytes(request["image"])
                result.append(("inspect", "vlm", {
                    "images": [name],
                    "messages": [
                        {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": request["prompt"]}]},
                        {"role": "assistant", "content": [{"type": "text", "text": json.dumps(request["reply"], ensure_ascii=False)}]},
                    ],
                    "teacher": teacher,
                }))
            return result

        revise = bool(profile.features) and _variant(product_id, "revise") < 4
        recover = {0: "check_first", 1: "bad_layout"}.get(_variant(product_id, "recover"))
        inspect = judge is not None and _variant(product_id, "inspect") < 3
        draft = draft_conversation(
            profile, hints, image, "image/jpeg", revise=revise, recover=recover,
            extra_images=extras, judge=judge, inspect=inspect,
        )
        if not judged_ok(draft.get("judge_examples", []), known):
            rows.append(("judge:bad-reply", "", {}))
            draft = draft_conversation(profile, hints, image, "image/jpeg", revise=revise, recover=recover, extra_images=extras)
        revise_example = draft.pop("revise_example", None)
        judged = draft.pop("judge_examples", [])
        rows.append(("agent_draft", "agent", draft))
        if revise_example:
            rows.append(("revise", "vlm", {"images": [image_name], **revise_example}))
        rows += judged_rows(judged)

        for template in pick_edit_templates(runtime_draft(profile, hints), hints, product_id, count=2):
            row = edit_conversation(profile, hints, image, "image/jpeg", template)
            row.pop("result")
            rows.append((f"agent_edit_{template.name}", "agent", row))

        if render:
            row = render_conversation(
                profile, hints, image, "image/jpeg", photo_generator=photo_generator, renderer=renderer, judge=judge,
            )
            if not judged_ok(row.get("judge_examples", []), known):
                rows.append(("judge:bad-reply", "", {}))
                row = render_conversation(profile, hints, image, "image/jpeg", photo_generator=photo_generator, renderer=renderer)
            judged = row.pop("judge_examples", [])
            rows.append(("agent_render", "agent", row))
            rows += judged_rows(judged)
        print(f"{product_id}: {len(rows)} rows", flush=True)
        return rows

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(conversations, jobs))

    # 3) Write in product order, so the files do not depend on which worker finished first.
    for job, rows in zip(jobs, results):
        product_id, category = job[0], job[1]
        for kind, file, row in rows:
            if not file:
                stats[kind] += 1
                continue
            write(kind, file, product_id, category, row)
        stats["products"] += 1

    summary = {key: value for key, value in sorted(stats.items(), key=str) if isinstance(key, str)}
    summary["groups"] = len(set(groups.values()))
    (out / "stats.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
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
    structure_parser = commands.add_parser("structure", help="Sort OCR sentences into roles with the teacher model.")
    structure_parser.set_defaults(run=structure)
    build_parser = commands.add_parser("build", help="Write analysis rows and agent conversations.")
    build_parser.add_argument("--max-per-group", type=int, default=5)
    build_parser.add_argument("--skip-render", action="store_true", help="Skip render conversations (no rembg/Playwright).")
    build_parser.add_argument("--no-judge", action="store_true", help="No inspect_* calls (no teacher image answers).")
    build_parser.add_argument("--image-url", default="http://127.0.0.1:11234")
    build_parser.add_argument("--image-model", default="ddalcu/Qwen-Image-2.1-MLX-Serve-4bit",
                              help="Image model of the final agent; empty for source photos only.")
    build_parser.add_argument("--image-steps", type=int, default=20, help="Qwen-Image 2.1 needs about 20 steps.")
    build_parser.set_defaults(run=build)
    for sub in (structure_parser, build_parser):
        sub.add_argument("--teacher", choices=("codex", "mlx"), default="codex", help="Labeling model for sorting and image judgments.")
        sub.add_argument("--codex-reasoning", default="xhigh")
        sub.add_argument("--codex-tier", default="priority", help="Codex service tier; priority is the Fast tier.")
        sub.add_argument("--workers", type=int, default=6, help="Parallel teacher calls.")
        sub.add_argument("--text-url", default="http://127.0.0.1:11234", help="MLX Serve, for --teacher mlx.")
        sub.add_argument("--text-model", default="ddalcu/Qwen3.8-27B-MLX-Serve-4bit", help="For --teacher mlx.")
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())

