import re
import unicodedata

from .dto import PageBlockDto, PageBlockItemDto, ProductProfileDto, UserHintsDto


SUPPORTED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
WEBP_SIGNATURE = b"RIFF"
_MAX_SELLER_SUBJECT_PREFIX_CHARS = 8
_RELATED_PRODUCT_PITCH = re.compile(
    r"(?<!\w)(?:다른|관련|연관|유사(?:한)?|비슷한)\s*"
    r"(?:(?:전통|수공예)\s*)?(?:상품|제품|공예품|작품)"
    r"|\b(?:other|related|similar)\s+"
    r"(?:(?:traditional|handmade)\s+)?(?:products?|items?|crafts?)\b",
    re.IGNORECASE,
)


class ValidationError(ValueError):
    """Base class for input and AI-output validation failures."""


class InvalidImageError(ValidationError):
    pass


class ProfileValidationError(ValidationError):
    pass


def validate_source_image(
    data: bytes,
    mime_type: str,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    require_decodable: bool = False,
) -> None:
    if mime_type not in SUPPORTED_MIME_TYPES:
        raise InvalidImageError(f"Unsupported image MIME type: {mime_type}")
    if not data:
        raise InvalidImageError("Image is empty")
    if len(data) > max_bytes:
        raise InvalidImageError("Image exceeds the maximum allowed size")

    signature_matches = (
        (mime_type == "image/png" and data.startswith(PNG_SIGNATURE))
        or (mime_type == "image/jpeg" and data.startswith(JPEG_SIGNATURE))
        or (
            mime_type == "image/webp"
            and len(data) >= 12
            and data.startswith(WEBP_SIGNATURE)
            and data[8:12] == b"WEBP"
        )
    )
    if not signature_matches:
        raise InvalidImageError("Image bytes do not match the declared MIME type")
    if require_decodable:
        try:
            from io import BytesIO
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                image.verify()
        except Exception as exc:
            raise InvalidImageError("Image bytes could not be decoded") from exc


def validate_product_profile(profile: ProductProfileDto) -> None:
    if not profile.product_type.strip():
        raise ProfileValidationError("product_type must not be empty")
    if not profile.summary.strip():
        raise ProfileValidationError("summary must not be empty")
    if len(profile.summary) > 500:
        raise ProfileValidationError("summary must be at most 500 characters")
    if len(profile.features) > 5:
        raise ProfileValidationError("features must contain at most 5 items")
    if len(profile.copy_sections) > 8:
        raise ProfileValidationError("copy_sections must contain at most 8 items")
    for feature in profile.features:
        if not 0 <= feature.confidence <= 1:
            raise ProfileValidationError("feature confidence must be between 0 and 1")


def ensure_editorial_page_plan(profile: ProductProfileDto) -> ProductProfileDto:
    """Fill required editorial beats that an image model may omit.

    The AI-authored copy and relative order remain authoritative. This function only
    moves the hero/closing to their required edges, applies the fidelity-sensitive
    image variants, and inserts missing reference-inspired blocks.
    """
    if not profile.page_plan:
        return profile

    product_name = profile.display_name or profile.product_type
    original = list(profile.page_plan)
    hero = next((block for block in original if block.block_type == "hero"), None)
    closing = next((block for block in original if block.block_type == "closing"), None)
    middle = [
        block
        for block in original
        if block.block_type not in {"hero", "closing"}
    ]

    if hero is None:
        hero = PageBlockDto(
            section_id="hero",
            block_type="hero",
            eyebrow="OBJECT DETAIL",
            title=product_name,
            body=profile.summary,
            variant="paper",
            photo_id="hero",
        )
    else:
        hero = hero.model_copy(
            update={"variant": hero.variant or "paper", "photo_id": "hero"}
        )

    if closing is None:
        closing = PageBlockDto(
            section_id="closing",
            block_type="closing",
            eyebrow="CRAFTSMANSHIP",
            title="공간에 오래 남는 인상",
            body=profile.summary,
            variant="paper",
        )
    elif not closing.variant:
        closing = closing.model_copy(update={"variant": "paper"})

    # The pilot models produced 8–10 blocks total, or 6–8 middle blocks after
    # hero/closing. At that density the model has supplied enough structure;
    # preserve its sequence and only apply the existing fidelity normalizations.
    if len(middle) >= 6:
        normalized_middle = []
        for block in middle:
            if block.block_type == "detail_split":
                block = block.model_copy(
                    update={
                        "variant": block.variant or "dark",
                        "photo_id": block.photo_id or "detail",
                    }
                )
            elif block.block_type == "usage_scene":
                block = block.model_copy(
                    update={
                        "variant": block.variant or "full-bleed",
                        "photo_id": "lifestyle",
                    }
                )
            elif block.block_type == "notice":
                if not block.variant:
                    block = block.model_copy(update={"variant": "dark"})
            normalized_middle.append(block)
        plan = [hero, *normalized_middle, closing]
        if len(plan) > 14:
            optional_types = {"wide_image", "scale_reference", "statement"}
            while len(plan) > 14:
                removable = next(
                    (
                        index
                        for index in range(len(plan) - 2, 0, -1)
                        if plan[index].block_type in optional_types
                    ),
                    None,
                )
                if removable is None:
                    break
                plan.pop(removable)
        return profile.model_copy(update={"page_plan": plan[:13] + [closing] if len(plan) > 14 else plan})

    def first_index(*block_types: str) -> int:
        return next(
            (
                index
                for index, block in enumerate(middle)
                if block.block_type in block_types
            ),
            len(middle),
        )

    detail_index = first_index("detail_split")
    if detail_index == len(middle):
        middle.insert(
            min(first_index("usage_scene", "gallery", "palette", "info_table"), len(middle)),
            PageBlockDto(
                section_id="surface-detail",
                block_type="detail_split",
                eyebrow="SURFACE DETAIL",
                title="가까이에서 본 표면과 형태",
                body=profile.summary,
                variant="dark",
                photo_id="detail",
            ),
        )
    else:
        middle[detail_index] = middle[detail_index].model_copy(
            update={
                "variant": middle[detail_index].variant or "dark",
                "photo_id": middle[detail_index].photo_id or "detail",
            }
        )

    usage_index = first_index("usage_scene")
    if usage_index == len(middle):
        detail_index = first_index("detail_split")
        middle.insert(
            detail_index + 1,
            PageBlockDto(
                section_id="usage-scene",
                block_type="usage_scene",
                eyebrow="EVERYDAY SCENE",
                title="일상에 놓인 공예의 풍경",
                body=profile.usage_scene or "제품이 자연스럽게 놓이는 장면을 제안합니다.",
                variant="full-bleed",
                photo_id="lifestyle",
            ),
        )
    else:
        middle[usage_index] = middle[usage_index].model_copy(
            update={
                "variant": middle[usage_index].variant or "full-bleed",
                "photo_id": "lifestyle",
            }
        )

    # A palette explains color/surface; it is not a substitute for the product
    # gallery. Keep both blocks when the model returns only one of them.
    if not any(block.block_type == "gallery" for block in middle):
        usage_index = first_index("usage_scene")
        middle.insert(
            usage_index + 1,
            PageBlockDto(
                section_id="product-gallery",
                block_type="gallery",
                eyebrow="PRODUCT GALLERY",
                title="색과 형태를 가까이에서",
                body="원본 이미지에서 확인되는 서로 다른 표면과 구성을 살펴보세요.",
                variant="paper",
                photo_ids=["detail", "detail-02", "detail-03", "detail-04"],
            ),
        )

    if not any(block.block_type == "recommendation" for block in middle):
        middle.insert(
            first_index("info_table", "notice"),
            PageBlockDto(
                section_id="recommended-scenes",
                block_type="recommendation",
                eyebrow="RECOMMENDED SCENES",
                title="이런 자리에서 더 잘 어울립니다.",
                body="제품의 색과 실루엣을 기준으로 제안하는 연출 아이디어입니다.",
                variant="paper",
                items=[
                    PageBlockItemDto(
                        label="01",
                        value="여백 있는 테이블",
                        description="주변 소품을 덜어 형태가 돋보이게 연출해 보세요.",
                        evidence="inferred",
                    ),
                    PageBlockItemDto(
                        label="02",
                        value="부드러운 자연광",
                        description="측면의 은은한 빛으로 표면의 결을 살려 보세요.",
                        evidence="inferred",
                    ),
                    PageBlockItemDto(
                        label="03",
                        value="차분한 배경색",
                        description="크림·베이지 계열 배경으로 제품에 시선을 모아 보세요.",
                        evidence="inferred",
                    ),
                ],
            ),
        )

    if not any(block.block_type == "info_table" for block in middle):
        components = profile.observations.get("visible_components", [])
        colors = profile.observations.get("colors", [])
        middle.insert(
            first_index("notice"),
            PageBlockDto(
                section_id="product-info",
                block_type="info_table",
                eyebrow="SPECIFICATIONS",
                title="주문 전 확인할 기본 정보",
                body="이미지에서 확인되는 범위만 정리했습니다.",
                variant="light",
                items=[
                    PageBlockItemDto(
                        label="구성",
                        value=(
                            ", ".join(str(value) for value in components[:6])
                            if isinstance(components, list) and components
                            else "확인 필요"
                        ),
                    ),
                    PageBlockItemDto(
                        label="색상",
                        value=(
                            ", ".join(str(value) for value in colors[:4])
                            if isinstance(colors, list) and colors
                            else "확인 필요"
                        ),
                    ),
                    PageBlockItemDto(label="소재·크기", value="확인 필요", evidence="unknown"),
                ],
            ),
        )

    notice_index = first_index("notice")
    if notice_index == len(middle):
        middle.append(
            PageBlockDto(
                section_id="care-notice",
                block_type="notice",
                eyebrow="CARE NOTE",
                title="구매 전 확인해 주세요.",
                body="관리법과 정확한 사양은 장인의 입력 내용으로 최종 확인해 주세요.",
                variant="dark",
            )
        )
    else:
        if not middle[notice_index].variant:
            middle[notice_index] = middle[notice_index].model_copy(update={"variant": "dark"})

    if len(middle) < 7 and not any(block.block_type == "statement" for block in middle):
        middle.insert(
            0,
            PageBlockDto(
                section_id="editorial-statement",
                block_type="statement",
                eyebrow="OBJECT STORY",
                title="형태와 표면이 만드는 조화",
                body=profile.summary,
                variant="paper",
            ),
        )

    plan = [hero, *middle, closing]
    if len(plan) > 14:
        optional_types = {"wide_image", "scale_reference", "statement"}
        while len(plan) > 14:
            removable = next(
                (
                    index
                    for index in range(len(plan) - 2, 0, -1)
                    if plan[index].block_type in optional_types
                ),
                None,
            )
            if removable is None:
                break
            plan.pop(removable)
    return profile.model_copy(update={"page_plan": plan[:13] + [closing] if len(plan) > 14 else plan})


def sanitize_page_plan(blocks: list[PageBlockDto]) -> list[PageBlockDto]:
    """Keep staging ideas, but omit empty or unbacked related-product pitches."""
    safe = []
    for block in blocks:
        block = PageBlockDto.model_validate(block)
        if block.block_type == "recommendation":
            # Match direct references within each field; never bridge a staging
            # heading (e.g. "추천 연출") to a product noun in another field.
            copy_fragments = [
                block.eyebrow, block.title, block.body,
                *(text for item in block.items for text in (item.label, item.value, item.description)),
            ]
            related_products = any(
                _RELATED_PRODUCT_PITCH.search(text) for text in copy_fragments
            )
            if not block.items or related_products:
                continue
        safe.append(block)
    return safe


def _seller_confirmed_statements(user_hints: UserHintsDto | None) -> set[str]:
    statements = set()
    if user_hints:
        for value in user_hints.model_dump(exclude_none=True).values():
            statements.add(_normalize_statement(value))
        statements.update(_seller_instruction_statements(user_hints))
    return statements - {""}


def _seller_instruction_statements(user_hints: UserHintsDto | None) -> set[str]:
    """Only making/care sentences may confirm a warning with an added subject."""
    statements = set()
    if user_hints:
        for value in (user_hints.making_method, user_hints.care_guide):
            if value:
                statements.update(
                    _normalize_statement(part) for part in _split_statements(value)
                )
    return statements - {""}


def _has_seller_subject_prefix(statement: str, instructions: set[str]) -> bool:
    for instruction in instructions:
        # The seller sentence must end the warning; added claims after it stay unknown.
        if not statement.endswith(instruction):
            continue
        prefix = statement[:-len(instruction)]
        if not prefix.endswith(" "):
            continue
        subject = prefix.rstrip()
        if (
            len(subject.replace(" ", "")) <= _MAX_SELLER_SUBJECT_PREFIX_CHARS
            and re.fullmatch(r"[\w· -]+(?:은|는|이|가|을|를)", subject)
        ):
            return True
    return False


def _split_statements(value: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?。！？])\s+|\n+", value) if part.strip()]


def _normalize_statement(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).rstrip(" .!?。！？")


def _compact(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value))


def is_grounded_copy(item, user_hints: UserHintsDto | None) -> bool:
    """Seen in the photo, or written by the artisan (found in user_hints, ignoring spacing)."""
    evidence = getattr(item, "evidence", "image-visible")
    if evidence == "image-visible":
        return True
    if evidence != "creator-provided" or user_hints is None:
        return False
    creator = _compact(" ".join(filter(None, [
        user_hints.product_name, user_hints.making_method, user_hints.care_guide,
    ])))
    statement = _compact(item.description)
    return bool(statement) and statement in creator


def sanitize_profile_for_render(
    profile: ProductProfileDto, *, user_hints: UserHintsDto | None = None,
) -> ProductProfileDto:
    """Keep uncertain model claims out of customer-facing selling copy."""
    safe_features = [
        feature for feature in profile.features if is_grounded_copy(feature, user_hints)
    ]
    safe_copy_sections = [
        section
        for section in profile.copy_sections
        if is_grounded_copy(section, user_hints)
    ]
    rejected = [
        (
            f"장인 입력에서 확인되지 않는 문구 제외: {item.title}"
            if item.evidence == "creator-provided"
            else f"검증되지 않은 AI 추정 문구 제외: {item.title}"
        )
        for item in [*profile.features, *profile.copy_sections]
        if not is_grounded_copy(item, user_hints)
    ]
    confirmed = _seller_confirmed_statements(user_hints)
    instructions = _seller_instruction_statements(user_hints)

    def confirmed_statement(value: str) -> bool:
        statement = _normalize_statement(value)
        return statement in confirmed or _has_seller_subject_prefix(statement, instructions)

    def unconfirmed(value: str) -> bool:
        if _normalize_statement(value) in confirmed:
            return False
        return not all(confirmed_statement(part) for part in _split_statements(value))

    warnings = list(dict.fromkeys(
        value for value in [*profile.uncertain_information, *rejected] if unconfirmed(value)
    ))
    return profile.model_copy(
        update={
            "features": safe_features,
            "copy_sections": safe_copy_sections,
            "uncertain_information": warnings[:12],
            "safety_notes": [value for value in profile.safety_notes if unconfirmed(value)],
            "page_plan": sanitize_page_plan(profile.page_plan),
        }
    )
