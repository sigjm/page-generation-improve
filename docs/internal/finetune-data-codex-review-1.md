# Codex 교차 검토 1: 파인튜닝 데이터 선정 기준 (2026-10-15)

`codex exec -s read-only`(codex-cli 0.160.1)로 받은 검토 원문입니다. 질문은 `docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md` 6장의 첫 번째 검토입니다.

**판정:** 분석 JSON과 실제 판매 문구를 쓰려는 방향은 형식 오류와 빈약한 한국어 문구를 겨냥합니다. 하지만 지금 규칙대로 만들면 판매자 제공 정보를 `image-visible`로 잘못 학습시키고, 모델이 겪은 도구 선택·오류 회복 실패는 대부분 예제에서 빠집니다. 입력 필드와 학습용 내보내기 형식도 먼저 맞춰야 합니다.

### 1. 실측 실패를 겨냥하는가?

**일부는 그렇고, 핵심 도구 실패는 놓칩니다.** 이미지와 실제 분석 프롬프트로 `ProductProfileDto` 정답을 만드는 예제는 JSON 파손·DTO 불일치를 겨냥합니다. `product_name`을 실제 힌트로 넣는다면 상품명 누락도 다룰 수 있고, OCR 원문 문구는 일반적인 한국어 카피 문제에도 도움이 됩니다 ([설계:35–40](/Users/jmllem/PycharmProjects/page-generation-improve/docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md:35), [분석 프롬프트:89–101](/Users/jmllem/PycharmProjects/page-generation-improve/src/local_detail_page_ai/adapters.py:89)).

반면 실측에서 4B는 상품명 누락, 오분류, 일반적인 문구 외에도 사진 ID를 섹션 이름처럼 사용해 검사 도구 호출에 실패했고, 초안에서 `revise_copy`와 사진 검사를 쓰지 않았습니다. 9B는 `inspect_render` 반복 호출로 턴 상한에 걸렸습니다 ([실측:65–69](/Users/jmllem/PycharmProjects/page-generation-improve/docs/internal/agent-tool-calling-smoke.md:65)). 그런데 설계된 초안 경로에는 `inspect_photo`와 `revise_copy`가 없고, 렌더 경로에도 `inspect_render`가 없습니다 ([설계:65–77](/Users/jmllem/PycharmProjects/page-generation-improve/docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md:65)).

추가로 같은 모델이 `revise_copy`, `inspect_photo`, 렌더 이미지 검사까지 담당한다면, 그 내부 JSON 호출을 가르칠 예제도 필요합니다. 현재 대화 루프의 `revise_copy` 결과는 적용 여부와 남은 대상만 돌려주고, 문구를 생성하는 내부 프롬프트는 별도 호출입니다 ([도구:178–202](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/tools.py:178), [내부 프롬프트:68–87](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/prompts.py:68)). 분석 JSON과 도구 라우팅 대화만으로는 이 호출을 직접 학습시키기 어렵습니다.

실측의 서버 메모리 거절은 별도입니다. 요청이 모델 처리 전에 거절되는 문제는 SFT 예제로 해결할 수 없습니다 ([실측:40–46](/Users/jmllem/PycharmProjects/page-generation-improve/docs/internal/agent-tool-calling-smoke.md:40)).

### 2. 고정 순서의 문제와 크롤링 데이터만으로 보완하는 방법

**고정 순서는 특정 도구를 건너뛰는 습관을 강화할 수 있습니다.** 실제 초안 프롬프트는 검사와 수정이 선택적인 순서를 권하지만, 설계 각본은 매번 `analyze → check → plan → finish`를 택합니다 ([프롬프트:13–20](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/prompts.py:13), [설계:65–77](/Users/jmllem/PycharmProjects/page-generation-improve/docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md:65)). 렌더 각본 역시 사진 검사와 재생성, 렌더 검사 경로를 보여주지 않습니다. 이는 실제 프롬프트의 선택적 동작 및 각 도구의 호출 제한과 차이가 납니다 ([프롬프트:22–30](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/prompts.py:22), [도구·호출 제한:382–394](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/tools.py:382)).

교사 모델이 새 상품 내용을 만들지 않아도 다음과 같이 경로를 늘릴 수 있습니다.

- 수집된 사진 수와 판매자 정보 유무에 따라 검사 생략·실행, 근거 부족 문구 수정 같은 **조건부 경로**를 만듭니다. 단, 도구 선후관계는 지킵니다. 예를 들어 `analyze_photos` 뒤에 근거 확인·수정을 하고, `render_page` 뒤에만 렌더 검사를 합니다.
- OCR 원문과 상품정보 표에서 그대로 가져온 문자열, 실제 DTO·도구 결과를 사용해 `check_copy_grounding → revise_copy` 예제를 추가합니다. `revise_copy` 내부 문구 생성까지 학습하려면 원문 구절을 활용한 별도 JSON 예제도 필요합니다.
- 실제 스키마 검증 오류와 없는 사진 ID 응답을 넣고, 다음 턴에서 유효한 ID로 재호출하거나 끝내는 추적을 만듭니다. 오류 응답은 도구가 반환하는 값에서 만들 수 있습니다. 도구 호출 상한에 도달한 뒤 도구 목록이 바뀌는 사례도 기록해야 합니다 ([루프:139–144](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/loop.py:139)).
- 편집에서는 구조 변경뿐 아니라 실제 `update_copy`의 지시·재표현·사용자 힌트·이미지 근거 및 거절 경로를 다룹니다. 현재 편집 도구는 이 근거 유형과 블록 조작을 지원합니다 ([편집 도구:397–440](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/tools.py:397), [편집 적용·거절:465–503](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/tools.py:465)).

`inspect_render`는 렌더 결과 이미지가 있어야 의미 있게 가르칠 수 있습니다. 크롤링 페이지 캡처만으로는 이 앱의 렌더 결과 검사와 같은 과제가 되지 않으므로, 실제 크롤링 문구·사진을 사용해 앱에서 결정적으로 렌더하는 방식이 더 적절합니다.

### 3. 근거 라벨과 `sanitize`가 맞는가?

**`판매자 문구 → evidence="image-visible"` 규칙은 고쳐야 합니다.** 이미지에서 보이지 않는 제작법·소재·관리 안내는 판매자가 제공한 정보일 수는 있어도 시각적 근거는 아닙니다. 현재 `ProductFeatureDto`와 `CopySectionDto`의 근거 값에는 `user-hints`가 없습니다 ([DTO:32–44](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/dto.py:32), [사용자 힌트 필드:123–145](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/dto.py:123)).

문제는 라벨이 실제 렌더 결과에 영향을 준다는 점입니다. `sanitize_profile_for_render`는 `features`와 `copy_sections`에서 `image-visible`만 남기므로, 판매자 주장을 그 라벨로 잘못 넣으면 그대로 통과할 수 있습니다. 사용자 힌트와의 문자열 대조는 불확실 정보·안전 문구에만 적용됩니다 ([정리 규칙:427–465](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/validation.py:427)). OCR 문자열 유사도 0.9 검사는 원문에 문구가 있다는 점을 확인할 뿐, 사진에서 확인되는 사실인지는 검증하지 않습니다 ([설계:59–63](/Users/jmllem/PycharmProjects/page-generation-improve/docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md:59)).

입력 필드도 맞지 않습니다. 설계는 상품정보 표의 소재·규격·구성·용도 등을 `info_table`과 장인 입력에 넣으려 하지만, 실제 `UserHintsDto`에는 제품명·제작법·관리법만 있습니다. `LocalProductAnalyzer`도 이미지와 이 힌트만 분석 호출에 전달합니다 ([설계:32–39](/Users/jmllem/PycharmProjects/page-generation-improve/docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md:32), [분석기 호출:128–141](/Users/jmllem/PycharmProjects/page-generation-improve/src/local_detail_page_ai/adapters.py:128)). 표에서 가져온 사실을 정답에 넣을 거라면 모델 입력에도 전달하거나, 전달되지 않는 사실은 해당 분석 정답에서 제외해야 합니다. 판매자 제공 정보와 사진 관찰을 데이터에서도 구분할 수 있도록 근거 표현을 정하는 것이 우선입니다.

### 4. 단일 출처 편향과 CMA 평가셋의 차이

편향은 **실제 위험이지만, CMA 평가셋과의 차이가 평가를 무효로 만들지는 않습니다.** KCDF 상품몰만 쓰고, 소재·용도 정보가 있으며 OCR 한글이 80자 이상인 상품만 고르면 사진과 설명이 풍부한 상품 페이지 쪽으로 표본이 기웁니다 ([출처:22–25](/Users/jmllem/PycharmProjects/page-generation-improve/docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md:22), [선정 기준:47–50](/Users/jmllem/PycharmProjects/page-generation-improve/docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md:47)). 한 판매몰의 문구와 페이지 형식에 익숙해지거나, OCR이 적은 상품에서 성능이 떨어질 수 있습니다.

CMA real v1은 60점의 역사적 소장품이며 6개 대분류에 각 10점입니다. 동일한 실물이 분석·렌더링 양쪽에 쓰이고, 판매 정보나 현대 상품 관리법을 평가하지 않습니다. 따라서 시각적 근거와 추측 억제, 원본 보존을 확인하는 보조 holdout으로는 유용하지만, 현대 판매 페이지 카피 품질을 대표하지는 않습니다 ([CMA 범위:7–13](/Users/jmllem/PycharmProjects/page-generation-improve/data/evaluation/cma_real_v1/README.md:7), [힌트·평가 절차:26–45](/Users/jmllem/PycharmProjects/page-generation-improve/data/evaluation/cma_real_v1/README.md:26)). 현재 CMA 데이터 설명도 시리즈·카테고리 적합성을 사람이 검수해야 한다고 명시합니다 ([검수 한계:53–55](/Users/jmllem/PycharmProjects/page-generation-improve/data/evaluation/cma_real_v1/README.md:53)).

상품 ID 기준으로 분리하는 계획은 적절합니다. 다만 같은 작가·시리즈·유사 상품이 train과 val에 나뉘지 않도록 가능하면 작가나 컬렉션 단위도 고려하세요 ([설계의 분할 기준:41–42](/Users/jmllem/PycharmProjects/page-generation-improve/docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md:41)).

### 5. OpenAI 채팅 JSONL과 mlx-vlm LoRA 형식 주의점

두 형식은 **하나의 JSONL로 바로 겸용된다고 가정하지 않는 편이 좋습니다.** OpenAI의 SFT 형식은 이미지 입력을 메시지 콘텐츠의 `image_url` 블록으로 표현하며, 함수 호출 예제는 `messages`와 `tools`를 사용합니다. 설계안의 최상위 `images`, `id`, `kind`, `product_id` 등은 학습용 파일과 별도 메타데이터로 분리하고, 각 백엔드가 요구하는 필드만 내보내는 편이 안전합니다 ([OpenAI SFT 형식](https://platform.openai.com/docs/api-reference/fine-tuning?lang=node)).

mlx-vlm은 Hugging Face 데이터셋의 `images`와 `messages`를 받고, 이미지 메시지의 표현이 모델별 형식과 일치해야 한다고 설명합니다. 문서의 Qwen 예시는 `{"type":"image","image": ...}` 형태입니다. OpenAI `tool_calls` 대화를 이 형식으로 자동 변환한다고 문서가 보장하지 않으므로, 사용하려는 mlx-vlm 버전·모델·서버 파서에서 도구 호출 왕복까지 확인해야 합니다 ([mlx-vlm LoRA 형식](https://github.com/Blaizzy/mlx-vlm/blob/main/mlx_vlm/LORA.MD)).

또한 실제 에이전트 루프의 입력 메시지는 텍스트 프롬프트로 시작하고, 분석·사진 검사는 도구 안에서 이미지와 함께 별도 호출됩니다 ([루프 메시지 생성:121–124](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/loop.py:121), [분석 도구 결과:169–173](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/tools.py:169), [사진 검사 내부 호출:140–153](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/tools.py:140)). 그러므로 모든 도구 라우팅 대화에 이미지까지 붙이면 실제 추론과 학습 입력이 달라질 수 있습니다. 이미지 분석, 도구 라우팅, 내부 검사 JSON을 별도 과제로 내보내세요.

마지막으로 매 턴 도구 목록이 달라질 수 있는데, 특히 호출 제한에 걸린 도구는 다음 요청부터 목록에서 빠집니다 ([루프:139–144](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/loop.py:139)). 한 대화 전체에 고정된 `tools` 하나만 저장하면 런타임과 불일치할 수 있습니다. 어시스턴트의 도구 호출만 학습 대상으로 마스킹할 수 있는지, 그리고 긴 프롬프트·대화가 모델의 최대 시퀀스 길이에 잘리지 않는지도 확인해야 합니다.

### 정리

**반드시 고칠 것**

- 판매자 문구를 `image-visible`로 라벨링하지 말고, 판매자 제공 정보와 시각적 근거를 구분할 방법을 정할 것.
- `info_table`의 판매자 정보가 실제 분석 모델 입력에 없는 문제를 해결할 것.
- `inspect_photo`, `revise_copy`, `inspect_render`, 오류 회복·호출 제한·거절 경로를 각본 대화에 포함할 것.
- OpenAI용과 mlx-vlm용 내보내기를 구분하고, 대상 모델에서 이미지와 도구 호출이 실제로 학습·추론되는지 확인할 것.

**고치면 좋은 것**

- 작가·시리즈·유사 상품 단위의 분할 누수 점검.
- 분석 JSON, 근거 오표기, 도구 인자 정확성, 종료 상한 초과 등을 각각 별도 지표로 평가.
- 내부 `revise_copy`·사진 검사 JSON 호출용 예제를 추가.

**그대로 둬도 되는 것**

- 실제 프롬프트와 DTO 스키마를 정답 기준으로 삼는 방향.
- 상품 ID로 같은 상품의 예제를 train/val에 함께 넣지 않으려는 계획.
- 개인 학습 자료를 로컬에 보관하고 재배포하지 않는 범위.

파일은 수정하지 않았고 테스트도 실행하지 않았습니다.