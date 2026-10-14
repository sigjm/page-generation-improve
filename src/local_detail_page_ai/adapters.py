from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from detail_page_ai.dto import ProductProfileDto, UserHintsDto
from detail_page_ai.layout_archetypes import select_layout_archetypes
from detail_page_ai.ports import ProductAnalyzer
from detail_page_ai.prompts import build_analysis_prompt

from .clients import LocalModelError, StructuredJsonChatClient


_COPY_SECTION_TYPES = {"hero", "benefit", "feature", "usage", "closing"}
_COPY_SECTION_ALIASES = {
    "recommendation": "benefit",
    "statement": "benefit",
    "detail_split": "feature",
    "wide_image": "feature",
    "gallery": "feature",
    "palette": "feature",
    "scale_reference": "feature",
    "info_table": "feature",
    "notice": "benefit",
}


def _normalize_local_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Repair the legacy copy-section label when a local model reuses page-plan names.

    page_plan is the authoritative adaptive layout. copy_sections is only the
    legacy copy projection, whose enum is intentionally smaller.
    """
    normalized = dict(payload)
    sections = normalized.get("copy_sections")
    if not isinstance(sections, list):
        return normalized
    normalized["copy_sections"] = [
        {
            **section,
            "section_type": (
                section.get("section_type")
                if section.get("section_type") in _COPY_SECTION_TYPES
                else _COPY_SECTION_ALIASES.get(section.get("section_type"), "feature")
            ),
        }
        if isinstance(section, dict)
        else section
        for section in sections
    ]
    return normalized


class LocalProductAnalyzer(ProductAnalyzer):
    """Creates the product profile and Korean copy with a local vision model."""

    def __init__(
        self,
        *,
        chat_client: StructuredJsonChatClient,
        locale: str = "ko-KR",
        image_generation_enabled: bool = False,
    ):
        self.chat_client = chat_client
        self.locale = locale
        self.image_generation_enabled = image_generation_enabled

    def _base_prompt(
        self,
        user_hints: UserHintsDto | None,
        image_sha256: str,
    ) -> tuple[str, dict[str, Any]]:
        schema = ProductProfileDto.model_json_schema()
        archetypes = select_layout_archetypes(
            image_sha256,
            user_hints,
            count=4,
            image_generation_enabled=self.image_generation_enabled,
        )
        analysis_prompt = build_analysis_prompt(
            self.locale,
            user_hints=user_hints,
            archetypes=archetypes,
            image_generation_enabled=self.image_generation_enabled,
        )
        prompt = (
            f"{analysis_prompt}\n"
            "입력된 제품명·제작과정·관리법은 장인이 제공한 상품 데이터이므로 상품별 "
            "제품명·제작 이야기·관리 안내 카피에 반드시 반영하라. 입력 데이터가 이미지보다 "
            "우선하며, 이미지와 다르게 보여도 입력 데이터를 기준으로 작성하라. 이미지에 "
            "보이지 않는다는 이유만으로 입력 데이터를 삭제하거나 이미지 추정으로 대체하지 "
            "마라. features는 표면 색·광택·문양·형태·구조처럼 실제 "
            "픽셀에서 확인되는 특징만 작성하고, 입력에 없는 보석/내용물/정확한 소재/제작기법/성능은 "
            "작성하지 마라. features와 copy_sections의 evidence는 사진에서 보이면 image-visible, "
            "입력된 장인 데이터에서 가져온 문장이면 creator-provided, 추정이면 inferred, "
            "모르면 unknown이다. creator-provided 문장은 입력 데이터의 표현을 그대로 써라.\n"
            "The following JSON Schema is authoritative. Every key and nested value "
            "must match it exactly; do not replace objects or arrays with strings.\n"
            f"JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
        )
        return prompt, schema

    def _generate_profile(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        image: bytes,
        mime_type: str,
    ) -> ProductProfileDto:
        payload = self.chat_client.generate_json(
            prompt,
            image=image,
            mime_type=mime_type,
            json_schema=schema,
        )
        try:
            return ProductProfileDto.model_validate(
                _normalize_local_profile_payload(payload)
            )
        except ValidationError as exc:
            raise LocalModelError(
                "Local vision model response does not match ProductProfileDto"
            ) from exc

    def analyze(
        self,
        image: bytes,
        mime_type: str,
        user_hints: UserHintsDto | None = None,
    ) -> ProductProfileDto:
        image_sha256 = hashlib.sha256(image).hexdigest()
        prompt, schema = self._base_prompt(user_hints, image_sha256)
        return self._generate_profile(
            prompt=prompt,
            schema=schema,
            image=image,
            mime_type=mime_type,
        )
