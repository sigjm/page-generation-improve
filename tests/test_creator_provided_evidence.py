from detail_page_ai.agent.tools import grounding_report
from detail_page_ai.dto import (
    ApprovedDraftDto,
    CopySectionDto,
    ProductFeatureDto,
    ProductProfileDto,
    UserHintsDto,
)
from detail_page_ai.validation import sanitize_profile_for_render

HINTS = UserHintsDto(
    product_name="합성 찻잔",
    making_method="흙을 손으로 빚어 1,250도에서 두 번 구웠습니다.",
    care_guide="전자레인지 사용은 피해 주세요.",
)


def profile() -> ProductProfileDto:
    return ProductProfileDto(
        product_type="찻잔",
        summary="손으로 빚은 찻잔입니다.",
        features=[
            ProductFeatureDto(title="두 번 구운 잔", description="흙을 손으로 빚어 1,250도에서 두 번 구웠습니다", evidence="creator-provided"),
            ProductFeatureDto(title="장인 작품", description="국가무형유산 보유자가 만들었습니다.", evidence="creator-provided"),
            ProductFeatureDto(title="푸른 유약", description="잔 안쪽에 푸른 유약이 보입니다.", evidence="image-visible"),
        ],
        copy_sections=[
            CopySectionDto(section_type="hero", title="합성 찻잔", description="흙을 손으로 빚어 1,250도에서 두 번 구웠습니다.", evidence="creator-provided"),
            CopySectionDto(section_type="feature", title="추정", description="오래 쓸수록 색이 깊어집니다.", evidence="inferred"),
        ],
    )


def test_creator_provided_copy_survives_only_when_the_creator_wrote_it():
    sanitized = sanitize_profile_for_render(profile(), user_hints=HINTS)

    assert [feature.title for feature in sanitized.features] == ["두 번 구운 잔", "푸른 유약"]
    assert [section.title for section in sanitized.copy_sections] == ["합성 찻잔"]
    assert any("장인 작품" in item for item in sanitized.uncertain_information)


def test_creator_provided_copy_without_hints_is_dropped():
    sanitized = sanitize_profile_for_render(profile(), user_hints=None)

    assert [feature.title for feature in sanitized.features] == ["푸른 유약"]
    assert sanitized.copy_sections == []


def test_draft_keeps_creator_provided_features():
    sanitized = sanitize_profile_for_render(profile(), user_hints=HINTS)

    draft = ApprovedDraftDto.from_profile(sanitized)

    assert [feature.title for feature in draft.features] == ["두 번 구운 잔", "푸른 유약"]


def test_grounding_report_mirrors_sanitize():
    report = grounding_report(profile(), HINTS)

    assert [item["target"] for item in report["ungrounded"]] == ["features.1", "copy_sections.1"]


def test_analysis_prompt_asks_for_creator_provided_labels():
    from local_detail_page_ai.adapters import LocalProductAnalyzer

    prompt, _ = LocalProductAnalyzer(chat_client=None)._base_prompt(HINTS, "0" * 64)

    assert "creator-provided" in prompt


def test_revise_prompt_offers_creator_provided():
    from detail_page_ai.agent.prompts import revise_copy_prompt

    prompt = revise_copy_prompt([], profile(), HINTS, [], "")

    assert "creator-provided" in prompt
