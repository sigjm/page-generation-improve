"""Turn a crawled craft product into the agent's gold ProductProfileDto.

Facts come from the seller's page (spec table, notice, OCR'd detail copy).
A model may only *sort* OCR sentences into roles; every sentence it returns
must be found in the OCR text, and short labels may use only words found there.
"""

import re
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..dto import (
    ApprovedDraftDto,
    CopySectionDto,
    PageBlockDto,
    PageBlockItemDto,
    ProductFeatureDto,
    ProductProfileDto,
    UserHintsDto,
)
from ..react_document_builder import build_react_document_from_draft
from ..validation import ensure_editorial_page_plan, sanitize_profile_for_render, validate_product_profile
from dataclasses import replace
from .crawl import ProductPage

BOILERPLATE = re.compile(
    r"연출(된)? ?이미지|A/S|고객센터|상담\s*시간|\d{3,4}-\d{4}|배송|교환|반품|copyright"
    r"|작가의 또 다른 상품|작가 페이지|^About$|^the Artist$"  # shop-wide banner under every product
    r"|^(19|20)\d{2}(?!\d)",  # artist CV lines ("2022 ... Fair (Seoul)") shared across a series
    re.I,
)
NOTICE_BOILERPLATE = re.compile(r"품질보증|소비자분쟁|A/S|고객센터|상담\s*시간|\d{3,4}-\d{4}|교환|반품|배송")
MAKING_KEYWORDS = (
    "제작", "공정", "손으로", "수작업", "빚", "가마", "옻칠", "자개", "엮", "염색",
    "바느질", "깎", "조각", "짜임", "직조", "상감", "유약", "마감", "소성", "땜",
)
MATERIAL_CRAFT = (
    ("자개", "나전칠기"), ("옻", "칠공예"), ("도자", "도자공예"), ("자기", "도자공예"),
    ("원목", "목공예"), ("나무", "목공예"), ("목재", "목공예"), ("금속", "금속공예"),
    ("은", "금속공예"), ("유리", "유리공예"), ("한지", "한지공예"), ("가죽", "가죽공예"),
    ("면", "섬유공예"), ("린넨", "섬유공예"), ("모시", "섬유공예"), ("견", "섬유공예"),
)
GALLERY_PHOTOS = ["packshot", "detail", "lifestyle"]
CREATOR = "creator-provided"  # the seller's own words, given to the model as user_hints


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text)


def clean_ocr_lines(lines: Sequence[str]) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    for line in lines:
        line = re.sub(r"\s+", " ", line).strip()
        key = _norm(line)
        if len(re.sub(r"[^가-힣A-Za-z0-9]", "", line)) < 2 or BOILERPLATE.search(line) or key in seen:
            continue
        seen.add(key)
        kept.append(line)
    return kept


CONTINUES = re.compile(r"(,|며|고|서|은|는|의|와|과|을|를|이|가|에|로|으로|하여|면서)$")


def join_sentences(lines: Sequence[str], limit: int = 300) -> list[str]:
    """Reconnect sentences that OCR split across lines (a line ending in a comma or a
    connective ending continues on the next line)."""
    sentences: list[str] = []
    pending = ""
    for line in lines:
        pending = f"{pending} {line}".strip() if pending else line
        if not CONTINUES.search(pending) or len(pending) >= limit:
            sentences.append(pending)
            pending = ""
    if pending:
        sentences.append(pending)
    return sentences


def build_user_hints(page: ProductPage, lines: Sequence[str], limit: int = 2000) -> UserHintsDto:
    """What the artisan would paste into the form: the seller copy, then the spec table.

    The gold may only use facts found here or in the photo, so the model never
    learns to state something its input did not contain.
    """
    spec_lines = [f"{key}: {value}" for key, value in page.spec.items() if value]
    budget = limit - sum(len(line) + 1 for line in spec_lines)
    kept: list[str] = []
    for line in lines:
        if budget - (len(line) + 1) < 0:
            break
        kept.append(line)
        budget -= len(line) + 1
    making = "\n".join([*kept, *spec_lines])[:limit]
    return UserHintsDto(
        product_name=page.name[:120] or None,
        making_method=making or None,
        care_guide=clean_notice(page.notice)[:1000] or None,
    )


def clean_notice(text: str) -> str:
    """The care notice without the legal and customer-service lines every product repeats."""
    return " ".join(sentence for sentence in _sentences(text) if not NOTICE_BOILERPLATE.search(sentence))


def hint_lines(hints: UserHintsDto) -> list[str]:
    """The text the analysis model receives, line by line."""
    lines = [hints.product_name or ""]
    lines += (hints.making_method or "").splitlines()
    lines.append(hints.care_guide or "")
    return [line for line in lines if line]


def grounded(text: str, source: Sequence[str]) -> bool:
    """True when ``text`` appears in the source lines, ignoring spaces and punctuation.

    No fuzzy matching: an OCR typo ("고측" for "곁을") must not pass as the seller's words.
    """
    target = _norm(text)
    return bool(target) and target in _norm("".join(source))


def grounded_label(text: str, source: Sequence[str]) -> bool:
    """A short label may reorder source words but not add new ones."""
    corpus = _norm("".join(source))
    words = [_norm(word) for word in text.split()]
    return bool(words) and all(word and word in corpus for word in words)


class Structured(BaseModel):
    """What the structuring model returns: OCR sentences sorted into roles."""

    model_config = ConfigDict(extra="ignore")

    product_type: str = ""
    hero_title: str = ""
    hero_description: str = ""
    summary: str = ""
    making: list[str] = Field(default_factory=list)
    features: list[dict[str, str]] = Field(default_factory=list)
    usage: list[str] = Field(default_factory=list)
    closing_title: str = ""
    closing_description: str = ""


def validate_structured(structured: Structured, source: Sequence[str], *, page: ProductPage) -> Structured:
    facts = [page.name, *page.spec.values(), *source]

    def sentence(text: str, limit: int) -> str:
        text = text.strip()
        if not text or not grounded(text, source):
            return ""
        target = _norm(text)
        whole = next((line for line in source if target and target in _norm(line)), text)
        text = whole if len(whole) <= limit else text
        return "" if len(text) > limit or text.endswith(",") else text

    def label(text: str, limit: int) -> str:
        text = text.strip()
        return text if text and len(text) <= limit and (grounded(text, source) or grounded_label(text, facts)) else ""

    features = []
    for item in structured.features[:5]:
        title = label(str(item.get("title", "")), 80)
        description = sentence(str(item.get("description", "")), 300)
        if title and description and description not in {f["description"] for f in features}:
            features.append({"title": title, "description": description})
    product_type = structured.product_type.strip()
    return Structured(
        product_type=product_type if product_type and len(product_type) <= 120 and grounded_label(product_type, facts) else "",
        hero_title=label(structured.hero_title, 80),
        hero_description=sentence(structured.hero_description, 300),
        summary=sentence(structured.summary, 500),
        making=[text for text in (sentence(item, 500) for item in structured.making) if text],
        features=features,
        usage=[text for text in (sentence(item, 300) for item in structured.usage) if text],
        closing_title=label(structured.closing_title, 80),
        closing_description=sentence(structured.closing_description, 300),
    )


def page_as_given(page: ProductPage, hints: UserHintsDto) -> ProductPage:
    """Keep only the spec rows the model will read, so info_table never states unseen facts."""
    given = set((hints.making_method or "").splitlines())
    return replace(page, spec={key: value for key, value in page.spec.items() if f"{key}: {value}" in given})


def available_blocks(page: ProductPage, structured: Structured) -> set[str]:
    blocks = {"hero", "wide_image"} if structured.hero_title and structured.hero_description else set()
    if structured.making:
        blocks.add("statement")
    if len(structured.features) >= 2:
        blocks.add("feature_grid")
    if structured.features or structured.making:
        blocks.add("detail_split")
    if page.extra_images:
        blocks.add("gallery")
    if structured.usage:
        blocks.add("usage_scene")
    if page.spec:
        blocks.add("info_table")
    if page.spec.get("규격"):
        blocks.add("scale_reference")
    if clean_notice(page.notice):
        blocks.add("notice")
    if structured.closing_title:
        blocks.add("closing")
    return blocks


def choose_archetype(candidates: Sequence[Mapping[str, Any]], available: set[str]) -> Mapping[str, Any]:
    """The candidate whose sequence we can fill the most of; ties keep prompt order."""
    return max(candidates, key=lambda arch: sum(block in available for block in arch["sequence"]))


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?。])\s+", text) if part.strip()]


def _block(block_type: str, variant: str, page: ProductPage, s: Structured) -> PageBlockDto | None:
    common = {"section_id": block_type, "block_type": block_type, "variant": variant}
    if block_type == "hero":
        return PageBlockDto(**common, title=s.hero_title, body=s.hero_description, photo_id="hero")
    if block_type == "statement" and s.making:
        return PageBlockDto(**common, body=" ".join(s.making)[:500])
    if block_type == "feature_grid" and len(s.features) >= 2:
        return PageBlockDto(**common, items=[
            PageBlockItemDto(label=f["title"], description=f["description"], evidence=CREATOR) for f in s.features
        ])
    if block_type == "detail_split" and (s.features or s.making):
        if s.features:
            return PageBlockDto(**common, title=s.features[0]["title"], body=s.features[0]["description"], photo_id="detail")
        return PageBlockDto(**common, body=s.making[0][:500], photo_id="detail")
    if block_type == "gallery" and page.extra_images:
        return PageBlockDto(**common, photo_ids=GALLERY_PHOTOS)
    if block_type == "wide_image":
        return PageBlockDto(**common, photo_id="packshot")
    if block_type == "usage_scene" and s.usage:
        return PageBlockDto(**common, body=s.usage[0], photo_id="lifestyle")
    if block_type == "info_table" and page.spec:
        return PageBlockDto(**common, items=[
            PageBlockItemDto(label=key, value=value[:180], evidence=CREATOR) for key, value in page.spec.items() if value
        ][:8])
    if block_type == "scale_reference" and page.spec.get("규격"):
        return PageBlockDto(**common, items=[PageBlockItemDto(label="규격", value=page.spec["규격"][:180], evidence=CREATOR)])
    if block_type == "notice" and clean_notice(page.notice):
        return PageBlockDto(**common, items=[
            PageBlockItemDto(label="유의사항", description=text[:300], evidence=CREATOR)
            for text in _sentences(clean_notice(page.notice))
        ][:8])
    if block_type == "closing" and s.closing_title:
        return PageBlockDto(**common, title=s.closing_title, body=s.closing_description)
    return None


def build_gold_profile(
    page: ProductPage,
    category: str,
    structured: Structured,
    archetype: Mapping[str, Any],
    hints: UserHintsDto,
) -> ProductProfileDto | None:
    s = structured
    page = page_as_given(page, hints)
    given = hint_lines(hints)
    sections: list[CopySectionDto] = []
    if s.hero_title and s.hero_description:
        sections.append(CopySectionDto(section_type="hero", title=s.hero_title, description=s.hero_description, evidence=CREATOR))
    for feature in s.features:
        sections.append(CopySectionDto(section_type="feature", title=feature["title"], description=feature["description"], evidence=CREATOR))
    closing = s.closing_description or s.closing_title
    if s.closing_title and grounded(closing, given):  # a label-only title is not the artisan's sentence
        sections.append(CopySectionDto(section_type="closing", title=s.closing_title, description=closing, evidence=CREATOR))
    summary = s.summary or s.hero_description or (s.making[0] if s.making else "")
    if len(sections) < 2 or not sections[0].section_type == "hero" or not summary:
        return None

    material = page.spec.get("소재", "")
    craft_type = next((craft for key, craft in MATERIAL_CRAFT if key in material), None)
    keywords = list(dict.fromkeys(
        word for word in [s.product_type, category, material, *re.split(r"\s*/\s*", page.spec.get("용도", ""))]
        if word and len(word) <= 40
    ))[:8]
    blocks = [
        block
        for block_type, variant in zip(archetype["sequence"], archetype["variants"], strict=True)
        if (block := _block(block_type, variant, page, s)) is not None
    ]
    return ProductProfileDto(
        product_type=s.product_type or category,
        display_name=page.name[:120] or None,
        is_traditional_craft="전통" in " ".join([s.hero_description, s.summary, *s.making]),
        craft_type=craft_type,
        classification_confidence=0.8,
        craft_confidence=0.8 if craft_type else 0.0,
        classification_reason=f"판매자 상품정보 소재: {material}"[:500] if material else "",
        layout_id="editorial-split",
        page_plan=blocks,
        summary=summary,
        keywords=keywords,
        observations={key: page.spec[key] for key in ("소재", "규격", "구성") if page.spec.get(key)},
        features=[
            ProductFeatureDto(title=f["title"], description=f["description"], evidence=CREATOR, confidence=0.8)
            for f in s.features
        ],
        copy_sections=sections,
        usage_scene=(s.usage[0] if s.usage else page.spec.get("용도", ""))[:300],
    )


def check_gold(profile: ProductProfileDto, user_hints: UserHintsDto) -> None:
    """The same checks the running service applies; raises when the gold would not survive them."""
    validate_product_profile(profile)
    planned = ensure_editorial_page_plan(profile)
    sanitized = sanitize_profile_for_render(planned, user_hints=user_hints)
    if sanitized.copy_sections != profile.copy_sections or sanitized.features != profile.features:
        raise ValueError("sanitize would drop gold copy")
    build_react_document_from_draft(ApprovedDraftDto.from_profile(sanitized))


def structure_prompt(page: ProductPage, lines: Sequence[str]) -> str:
    """Ask a model to sort the seller's own sentences into roles, never to write new ones."""
    spec = "\n".join(f"{key}: {value}" for key, value in page.spec.items())
    numbered = "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))
    return (
        "아래는 한 공예 상품 판매 페이지에서 OCR로 읽은 문장과 판매자가 적은 상품 정보다.\n"
        "문장을 새로 쓰지 말고, 아래 문장 중에서 골라 역할별로 나눠라. "
        "허용되는 수정은 줄바꿈으로 끊긴 한 문장을 이어 붙이는 것뿐이다. 원문에 없는 내용은 절대 넣지 마라.\n"
        "- product_type: 상품 종류를 나타내는 짧은 이름. 상품명이나 문장에 있는 단어만 쓴다.\n"
        "- hero_title: 페이지 제목으로 쓸 짧은 문구(80자 이내)\n"
        "- hero_description: 상품을 소개하는 대표 문장(300자 이내)\n"
        "- summary: 상품 요약 문장(500자 이내)\n"
        "- making: 재료, 제작 과정, 기법을 설명하는 문장들(있는 만큼 모두)\n"
        "- features: 형태, 질감, 문양, 색, 구성처럼 상품의 특징을 설명하는 문장. 특징마다 하나씩, 문장이 있으면 2~4개. "
        "title은 그 문장 속 단어로 만든 짧은 이름, description은 그 문장\n"
        "- usage: 쓰임새나 사용 장면을 설명하는 문장들\n"
        "- closing_title, closing_description: 페이지를 마무리할 문구. 마지막 맺음 문장이나 상품을 한 줄로 정리한 문구\n"
        "한 문장을 여러 칸에 써도 된다. 해당하는 문장이 정말 없을 때만 빈 문자열이나 빈 배열로 둔다.\n"
        f"<product_name>\n{page.name}\n</product_name>\n"
        f"<spec>\n{spec}\n</spec>\n"
        f"<lines>\n{numbered}\n</lines>\n"
        'JSON으로만 답하라: {"product_type": "", "hero_title": "", "hero_description": "", "summary": "", '
        '"making": [], "features": [{"title": "", "description": ""}], "usage": [], '
        '"closing_title": "", "closing_description": ""}'
    )
