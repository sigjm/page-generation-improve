from detail_page_ai.dto import ProductProfileDto
from detail_page_ai.finetune.crawl import ProductPage
from detail_page_ai.finetune.gold import (
    Structured,
    build_gold_profile,
    build_user_hints,
    check_gold,
    hint_lines,
    choose_archetype,
    clean_ocr_lines,
    grounded,
    validate_structured,
)

# Synthetic copy written for these tests (no crawled seller text).
LINES = [
    "결을 잇는 나무 상자",
    "결을 잇는 나무 상자",
    "서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자입니다.",
    "자투리 목재를 손으로 깎아 이어 붙이고 옻칠로 마감했습니다.",
    "뚜껑은 미닫이 방식으로 열립니다.",
    "책상 위 소품을 담아 두기 좋습니다.",
    "오래 곁에 두는 생활 오브제",
    "연출된 이미지입니다.",
    "A/S 문의 1588-0000",
    "ㅋ",
]
PAGE = ProductPage(
    name="합성 나무 상자",
    main_image="https://example.invalid/main.jpg",
    spec={"소재": "원목", "규격": "W120 x D80 x H60mm", "구성": "상자 1", "용도": "소품 보관 / 인테리어"},
    notice="원목 특성상 결과 색이 조금씩 다릅니다. 직사광선을 피해 보관해 주세요.",
)


def test_clean_ocr_lines_drops_repeats_boilerplate_and_noise():
    assert clean_ocr_lines(LINES) == [
        "결을 잇는 나무 상자",
        "서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자입니다.",
        "자투리 목재를 손으로 깎아 이어 붙이고 옻칠로 마감했습니다.",
        "뚜껑은 미닫이 방식으로 열립니다.",
        "책상 위 소품을 담아 두기 좋습니다.",
        "오래 곁에 두는 생활 오브제",
    ]


def test_user_hints_carry_the_seller_copy_and_spec_the_gold_may_use():
    hints = build_user_hints(PAGE, clean_ocr_lines(LINES))

    assert hints.product_name == "합성 나무 상자"
    assert hints.making_method.splitlines() == [
        *clean_ocr_lines(LINES),
        "소재: 원목",
        "규격: W120 x D80 x H60mm",
        "구성: 상자 1",
        "용도: 소품 보관 / 인테리어",
    ]
    assert hints.care_guide == PAGE.notice
    assert hint_lines(hints)[0] == "합성 나무 상자"


def test_user_hints_cut_long_copy_at_a_line_boundary():
    lines = [f"{index}번째 문장은 나무 결을 설명합니다." for index in range(200)]

    making = build_user_hints(PAGE, lines).making_method

    assert len(making) <= 2000
    assert all(line in lines or ":" in line for line in making.splitlines())


def test_grounded_accepts_verbatim_and_near_verbatim_text_only():
    source = clean_ocr_lines(LINES)

    assert grounded("서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자입니다", source)
    assert grounded("서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자 입니다.", source)
    assert grounded("뚜껑은 미닫이 방식으로 열립니다. 책상 위 소품을 담아 두기 좋습니다.", source)
    assert not grounded("100년 전통 장인이 만든 명품 상자입니다.", source)


def test_validate_structured_drops_invented_text():
    source = clean_ocr_lines(LINES)
    structured = Structured(
        product_type="나무 상자",
        hero_title="결을 잇는 나무 상자",
        hero_description="서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자입니다.",
        summary="조선 왕실에서 쓰던 상자를 재현했습니다.",
        making=["자투리 목재를 손으로 깎아 이어 붙이고 옻칠로 마감했습니다.", "국가무형유산 보유자가 만들었습니다."],
        features=[
            {"title": "미닫이 뚜껑", "description": "뚜껑은 미닫이 방식으로 열립니다."},
            {"title": "평생 보증", "description": "평생 무상 수리를 보장합니다."},
        ],
        usage=["책상 위 소품을 담아 두기 좋습니다."],
        closing_title="오래 곁에 두는 생활 오브제",
        closing_description="",
    )

    kept = validate_structured(structured, hint_lines(build_user_hints(PAGE, source)), page=PAGE)

    assert kept.summary == ""
    assert kept.making == ["자투리 목재를 손으로 깎아 이어 붙이고 옻칠로 마감했습니다."]
    assert [feature["title"] for feature in kept.features] == ["미닫이 뚜껑"]


def test_choose_archetype_prefers_the_one_we_can_fill():
    candidates = [
        {"name": "색", "sequence": ["hero", "palette", "recommendation", "closing"], "variants": ["paper", "light", "sand", "paper"]},
        {"name": "사양", "sequence": ["hero", "info_table", "notice", "closing"], "variants": ["paper", "light", "dark", "paper"]},
    ]

    assert choose_archetype(candidates, {"hero", "info_table", "notice", "closing"})["name"] == "사양"


def test_gold_profile_follows_the_archetype_and_passes_runtime_checks():
    source = clean_ocr_lines(LINES)
    structured = validate_structured(
        Structured(
            product_type="나무 상자",
            hero_title="결을 잇는 나무 상자",
            hero_description="서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자입니다.",
            summary="서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자입니다.",
            making=["자투리 목재를 손으로 깎아 이어 붙이고 옻칠로 마감했습니다."],
            features=[{"title": "미닫이 뚜껑", "description": "뚜껑은 미닫이 방식으로 열립니다."}],
            usage=["책상 위 소품을 담아 두기 좋습니다."],
            closing_title="오래 곁에 두는 생활 오브제",
            closing_description="",
        ),
        hint_lines(build_user_hints(PAGE, source)),
        page=PAGE,
    )
    archetype = {
        "name": "제작 서사 선행형",
        "sequence": ["hero", "statement", "gallery", "detail_split", "usage_scene", "info_table", "notice", "closing"],
        "variants": ["paper", "sand", "light", "image-left", "full-bleed", "light", "dark", "paper"],
    }

    hints = build_user_hints(PAGE, source)
    profile = build_gold_profile(PAGE, "인테리어", structured, archetype, hints)

    assert isinstance(profile, ProductProfileDto)
    assert profile.display_name == "합성 나무 상자"
    assert [block.block_type for block in profile.page_plan] == [
        "hero", "statement", "detail_split", "usage_scene", "info_table", "notice", "closing",
    ]
    assert [block.variant for block in profile.page_plan] == [
        "paper", "sand", "image-left", "full-bleed", "light", "dark", "paper",
    ]
    info = next(block for block in profile.page_plan if block.block_type == "info_table")
    assert [(item.label, item.value) for item in info.items][:2] == [("소재", "원목"), ("규격", "W120 x D80 x H60mm")]
    assert all(section.evidence == "image-visible" for section in profile.copy_sections)
    check_gold(profile, hints)


def test_gold_profile_is_skipped_without_enough_grounded_copy():
    structured = Structured(product_type="", hero_title="", hero_description="")

    archetype = {"name": "x", "sequence": ["hero", "closing"], "variants": ["paper", "paper"]}
    assert build_gold_profile(PAGE, "인테리어", structured, archetype, build_user_hints(PAGE, [])) is None


def test_spec_rows_cut_from_the_input_stay_out_of_the_gold():
    source = clean_ocr_lines(LINES)
    hints = build_user_hints(PAGE, source).model_copy(update={"making_method": "\n".join(source)})
    structured = validate_structured(
        Structured(
            hero_title="결을 잇는 나무 상자",
            hero_description="서로 다른 나무를 이어 붙여 무늬를 만든 작은 상자입니다.",
            features=[{"title": "미닫이 뚜껑", "description": "뚜껑은 미닫이 방식으로 열립니다."}],
            closing_title="오래 곁에 두는 생활 오브제",
        ),
        hint_lines(hints),
        page=PAGE,
    )
    archetype = {"name": "사양", "sequence": ["hero", "info_table", "scale_reference", "closing"], "variants": ["paper", "light", "sand", "paper"]}

    profile = build_gold_profile(PAGE, "인테리어", structured, archetype, hints)

    assert [block.block_type for block in profile.page_plan] == ["hero", "closing"]


def test_structure_prompt_lists_lines_and_forbids_new_text():
    from detail_page_ai.finetune.gold import structure_prompt

    prompt = structure_prompt(PAGE, ["첫 문장입니다.", "둘째 문장입니다."])

    assert "1. 첫 문장입니다." in prompt and "2. 둘째 문장입니다." in prompt
    assert "소재: 원목" in prompt
    assert "새로 쓰지 말고" in prompt
    assert '"hero_title"' in prompt


def test_clean_ocr_lines_drops_shop_banners_and_artist_cv_lines():
    lines = [
        "작가의 또 다른 상품, 작가 페이지에서 만나보세요.",
        "2022 합성 디자인 페어 (서울, Korea)",
        "1999년 합성 공방 설립",
        "나무 결을 살려 손으로 다듬었습니다.",
    ]

    assert clean_ocr_lines(lines) == ["나무 결을 살려 손으로 다듬었습니다."]
