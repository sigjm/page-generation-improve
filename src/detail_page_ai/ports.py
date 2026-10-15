from typing import Protocol

from .assets import StoredAsset
from .dto import CraftResearchDto, GenerationOptions, ProductProfileDto, UserHintsDto
from .models import GeneratedImage, ProductPhotoSet


class ModelCallError(RuntimeError):
    """A model call failed or returned output the service cannot use."""


class BackendSink(Protocol):
    def persist(self, request, image: GeneratedImage):
        ...


class SourceAssetStore(Protocol):
    def put(self, data: bytes, mime_type: str, category: str) -> StoredAsset:
        ...

    def get(self, asset_id: str) -> bytes:
        ...

    def get_record(self, asset_id: str) -> StoredAsset:
        ...

    def exists(self, asset_id: str) -> bool:
        ...


class ProductAnalyzer(Protocol):
    def analyze(
        self,
        image: bytes,
        mime_type: str,
        user_hints: UserHintsDto | None = None,
    ) -> ProductProfileDto:
        ...


class CraftResearcher(Protocol):
    def research(
        self,
        profile: ProductProfileDto,
        user_hints: UserHintsDto | None = None,
    ) -> CraftResearchDto:
        ...


class ProductPhotoGenerator(Protocol):
    def generate(
        self,
        *,
        source_image: bytes,
        source_mime_type: str,
        profile: ProductProfileDto,
        options: GenerationOptions,
        additional_source_images: tuple[tuple[bytes, str], ...] = (),
    ) -> ProductPhotoSet:
        ...


class DetailPageRenderer(Protocol):
    def render(
        self,
        source_image: bytes,
        source_mime_type: str,
        template_image: bytes | None,
        profile: ProductProfileDto,
        options: GenerationOptions,
        photo_set: ProductPhotoSet | None = None,
    ) -> GeneratedImage:
        ...
