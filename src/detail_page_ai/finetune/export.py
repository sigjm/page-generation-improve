"""Group, split and write the fine-tuning rows."""

import hashlib
import json
import re
from typing import Any

from ..dto import ProductProfileDto, UserHintsDto


def group_products(
    products: dict[str, dict[str, list[str]]], brands: dict[str, str] | None = None,
) -> dict[str, str]:
    """Products by the same artist, or sharing a detail image with real text (an artist bio,
    a series story), form one group, so near-duplicates never land on both sides of the split.

    ``products`` maps product id → {image sha256 → cleaned OCR lines of that image}.
    Images whose cleaned text is empty (shop-wide banners) and empty brands do not link products.
    """
    parent = {product_id: product_id for product_id in products}

    def root(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    owner: dict[str, str] = {}
    for product_id, images in sorted(products.items()):
        brand = (brands or {}).get(product_id, "")
        for image_hash, lines in [*images.items(), *([(f"brand:{brand}", [brand])] if brand else [])]:
            if not lines:
                continue
            if image_hash in owner:
                a, b = root(owner[image_hash]), root(product_id)
                parent[max(a, b)] = min(a, b)
            else:
                owner[image_hash] = product_id
    return {product_id: f"group-{root(product_id)}" for product_id in products}


def split_for(group: str, val_every: int = 10) -> str:
    return "val" if int(hashlib.sha256(group.encode()).hexdigest(), 16) % val_every == 0 else "train"


def analysis_row(image_path: str, image: bytes, profile: ProductProfileDto, hints: UserHintsDto) -> dict[str, Any]:
    """mlx-vlm row: the exact prompt LocalProductAnalyzer sends, answered with the gold profile."""
    from local_detail_page_ai.adapters import LocalProductAnalyzer

    prompt, _ = LocalProductAnalyzer(chat_client=None)._base_prompt(hints, hashlib.sha256(image).hexdigest())
    answer = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False)
    return {
        "images": [image_path],
        "messages": [
            {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]},
            {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        ],
    }


def judged_ok(requests: list[dict], known: str = "") -> bool:
    """Judge replies we would train on: a non-empty answer, a valid verdict field, and any
    quoted text spelled as the artisan wrote it (the judge sometimes misreads rendered text)."""
    compact = re.sub(r"\W+", "", known)

    def ok(reply: dict) -> bool:
        if not isinstance(reply.get("answer"), str) or not reply["answer"].strip():
            return False
        quoted = re.findall(r"['‘\"“]([^'’\"”]{2,40})['’\"”]", reply["answer"])
        if any(re.sub(r"\W+", "", text) not in compact for text in quoted):
            return False
        return reply.get("evidence") in {"image-visible", "inferred"} or isinstance(reply.get("has_problem"), bool)

    return all(ok(request["reply"]) for request in requests)
