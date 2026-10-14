"""Group, split and write the fine-tuning rows."""

import hashlib
import json
from typing import Any

from ..dto import ProductProfileDto, UserHintsDto


def group_products(products: dict[str, dict[str, list[str]]]) -> dict[str, str]:
    """Products that share a detail image with real text (an artist bio, a series story)
    form one group, so near-duplicates never land on both sides of the split.

    ``products`` maps product id → {image sha256 → cleaned OCR lines of that image}.
    Images whose cleaned text is empty (shop-wide banners) do not link products.
    """
    parent = {product_id: product_id for product_id in products}

    def root(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    owner: dict[str, str] = {}
    for product_id, images in sorted(products.items()):
        for image_hash, lines in images.items():
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
