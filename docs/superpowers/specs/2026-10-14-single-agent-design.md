# 고정 파이프라인 → 단일 AI 에이전트 전환 설계

- 작성일: 2026-10-14
- 기준 코드: `sigjm/page-generation` `main` (`c5dc2a7`)
- 대상 레포: `sigjm/page-generation-improve` (Private)

## 1. 목표와 범위

`DetailPagePipeline`의 고정 순서(분석 → 정리 → 사진 처리 → 검증 → 렌더)를, 자체 호스팅 Qwen이 도구를 골라 쓰는 **단일 에이전트**로 바꾼다.

| 목표 | 이 설계에서의 의미 |
|---|---|
| 품질 자기교정 | 근거 부족 문구, 어색한 생성 컷, 렌더 결함을 에이전트가 감지하고 해당 단계만 다시 시도 |
| 유연한 흐름 | 상품마다 필요한 도구를 에이전트가 고름. 예: 추가 사진을 골라서 봄 |
| 포트폴리오/학습 | 루프, 도구, 가드레일이 코드에서 분명히 구분되어 보임 |
| 대화형 편집 | 장인의 자연어 지시로 초안 수정 |

**바꾸지 않는 것**
- BE 계약: 엔드포인트, 상태값, 콜백 형식, `react_document`
- `DetailPageJobService`, outbox, lease, 재전송
- 세 가지 원칙
  1. 원본 사진을 다시 그리지 않음
  2. 없는 사실을 지어내지 않음
  3. 장인이 승인한 것만 최종 결과가 됨

**새로 추가하는 BE 대상 기능**: 대화형 편집 엔드포인트 1개 (5장)

## 2. 결정 사항

| 항목 | 결정 | 이유 |
|---|---|---|
| 에이전트 LLM | 자체 호스팅 Qwen3.8-27B (SGLang / MLX Serve의 OpenAI 호환 tool calling) | 외부 호출 없음 원칙 유지, 기존 배포 유지 |
| 구현 방식 | 직접 만든 tool-calling 루프. 프레임워크는 쓰지 않음 | 새 의존성 없이 기존 `MlxServeChatClient`(urllib)에 `tools`만 추가 |
| BE 계약 | 그대로 유지 | 기존 테스트를 회귀 검사로 재사용 |
| 레포 | 원본 히스토리를 포함해 복사하고, `upstream` 리모트로 원본 연결 | 전환 과정이 diff로 남음 |
| 에이전트 수 | 하나. 모드(초안 / 렌더 / 편집)에 따라 쓸 수 있는 도구만 다름 | 초안과 렌더 사이에 사람의 승인이 있어서 한 번에 이어서 돌릴 수 없음 |

## 3. 구조

```text
BE ─▶ FastAPI(app.py) ─▶ DetailPageJobService(service.py)        ← 변경 없음
                                │
                                ▼
                     DetailPageAgent(DetailPagePipeline 상속)
                     ├─ _compose_profile()  mode="draft"   (create_draft, run이 부르는 훅)
                     ├─ _produce_page()     mode="render"  (run이 부르는 훅)
                     └─ edit_draft()        mode="edit"    (신규)
                                │
                     ┌──────────┴───────────┐
                     ▼                      ▼
              agent/loop.py           agent/tools.py
              루프 · AgentState        모드별 도구 레지스트리
              예산 · 폴백              (기존 모듈을 감쌈)
                                │
                     필수 후처리 (에이전트가 건너뛸 수 없음)
                     sanitize → fidelity 검증 → react_document 검증 → outbox/BE 전달
```

### 3.1 파일 배치

| 파일 | 내용 |
|---|---|
| `src/detail_page_ai/agent/loop.py` | `AgentState`, `run_agent_loop()`, 예산, 폴백 신호 |
| `src/detail_page_ai/agent/tools.py` | 도구 정의(이름, 설명, Pydantic 인자 모델, 실행 함수)와 모드별 레지스트리 |
| `src/detail_page_ai/agent/prompts.py` | 모드별 시스템 프롬프트와 검수·재작성 프롬프트 |
| `src/detail_page_ai/agent/detail_page_agent.py` | `DetailPageAgent`: `_compose_profile`, `_produce_page` 훅 재정의, `edit_draft`, 폴백 연결 |
| `src/detail_page_ai/pipeline.py` (수정) | `create_draft`와 `run`의 단계 실행을 `_compose_profile`, `_produce_page` 훅으로 분리. 동작은 그대로 |
| `src/local_detail_page_ai/clients.py` (수정) | `MlxServeChatClient.chat_with_tools(messages, tools)` 추가 |
| `src/local_detail_page_ai/factory.py` (수정) | `AGENT_MODE`에 따라 `DetailPageAgent` 또는 `DetailPagePipeline` 생성 |
| `src/detail_page_ai/source_photos.py` (수정) | 생성 컷 역할 하나만 다시 만드는 `regenerate_role()` 추가 |
| `src/local_detail_page_ai/runner.py` (수정) | 생성기에 `direction` 인자 추가, `build_local_pipeline(agent_mode=...)` |
| `scripts/run_eval_pilot.py` (수정) | `--agent-mode`, 사례별 `agent_trace.json` 저장 |
| `src/detail_page_ai/service.py`, `app.py` (수정) | 편집 엔드포인트와 `edit_draft` 서비스 메서드 추가. `_run_job`이 `create_draft`에 `additional_source_images`도 넘기게 수정 (지금은 대표 사진만 넘김) |
| `deploy/sglang/entrypoint.sh` (수정) | 텍스트 서버에 `--tool-call-parser` 추가 |

### 3.2 루프 동작

1. 메시지는 다음 셋으로 시작한다.
   - 시스템 프롬프트: 원칙 + 모드별 목표 + 종료 조건
   - 작업 맥락: 사진 수, `user_hints`, 현재 `AgentState` 요약
   - 모드별 도구 스키마
2. 모델이 `tool_calls`를 반환하면 다음 순서로 처리한다.
   - 인자를 Pydantic으로 검증한다.
   - 도구를 실행한다.
   - 결과를 **요약 JSON**(최대 약 1KB)으로 `tool` 메시지에 붙인다.
3. 이미지 바이트, profile 전체, PNG는 대화에 넣지 않는다. `AgentState`에 보관하고 ID로만 참조한다. 텍스트 컨텍스트가 16,384 토큰이기 때문이다.
4. 에이전트가 `finish`를 호출하면 종료한다.
5. 매 도구 실행 직전에 해당 도구에 매핑된 기존 상태값을 `status_callback`으로 보낸다. BE가 보는 상태값은 바뀌지 않는다.
6. 루프 호출은 `temperature=0`으로 한다.

**도구별 상태값 매핑**

| 상태값 | 도구 |
|---|---|
| `ANALYZING` | `analyze_photos`, `inspect_photo`, `revise_copy` (`check_copy_grounding`은 모델 호출이 없어 상태를 바꾸지 않음) |
| `EXTRACTING` | `plan_page` |
| `GENERATING_BACKGROUNDS` | `prepare_photos`, `regenerate_photo` |
| `VERIFYING` | `verify_photos`, `inspect_image` |
| `RENDERING` | `render_page`, `inspect_render` |

### 3.3 예산과 설정

| 설정 | 기본값 | 의미 |
|---|---|---|
| `AGENT_MODE` | `agent` | `pipeline`이면 기존 `DetailPagePipeline`을 그대로 씀 |
| `AGENT_MAX_TOOL_CALLS` | `15` | 한 번 실행할 때의 도구 호출 상한 |
| `AGENT_TIMEOUT_SECONDS` | `600` | 루프 전체 제한 시간 |
| (고정) 같은 도구 연속 실패 | 3회 | 넘으면 루프 중단 후 폴백 |
| (고정) `revise_copy` | 2회 | |
| (고정) `render_page` | 2회 | |
| (고정) `inspect_render` | 2회 | |

## 4. 모드별 도구

### 4.1 초안 모드 (`create_draft`)

사진 생성과 PNG 렌더는 하지 않는다.

| 도구 | 감싸는 코드 | 하는 일 |
|---|---|---|
| `analyze_photos` | `LocalProductAnalyzer.analyze` | 대표 사진과 `user_hints`로 profile 생성 |
| `inspect_photo(index, question)` | `chat_client.generate_json` + 이미지 (신규) | 추가 사진 중 하나를 골라 질문에 답함. 답에는 근거 종류(`image-visible` / `inferred`)를 붙임 |
| `check_copy_grounding` | `sanitize_profile_for_render` dry-run | 지금 렌더하면 빠질 문구 목록과 이유 |
| `revise_copy(section_ids, instruction)` | Qwen 호출 (신규) | 빠질 문구를 사진이나 `user_hints` 근거 안에서 다시 씀. 결과의 `evidence`는 모델이 표시하고, sanitize가 다시 거름 |
| `plan_page(layout_id?)` | `ensure_editorial_page_plan` | 레이아웃과 블록 구성 확정. 보정된 내용 보고 |
| `finish(summary)` | — | 종료 |

**공예 조사 도구는 넣지 않는다.**
- 원본에는 `CraftResearcher` Protocol과 `build_craft_research_prompt`만 있고, 실제 구현체가 없으며 `factory.py`에서도 연결되지 않는다. 운영에서 한 번도 실행되지 않는 경로다.
- 외부 검색 없이 모델 지식만으로 "조사"하면 근거 없는 사실을 만들 위험이 있다.
- 공예 맥락은 `user_hints`와 분석 프롬프트가 담당한다.

### 4.2 렌더 모드 (`run`, 승인 후)

`AgentState.approved_draft`(문구와 `page_plan`)는 frozen이다. 렌더 도구에는 문구나 블록을 받는 인자가 없다.

| 도구 | 감싸는 코드 | 하는 일 |
|---|---|---|
| `prepare_photos` | `SourcePreservingProductPhotoGenerator.generate` | 누끼, 역할 배정, FLUX 보조 컷 생성 |
| `verify_photos` | `ProductFidelityValidator.validate` + 누끼 게이트 결과 | 사진별 상태와 이유 |
| `inspect_image(photo_id, question)` | Qwen VLM (신규) | 생성 컷의 제품 왜곡, 공예 종류와 맞지 않는 배경 확인 |
| `regenerate_photo(photo_id, direction)` | `SourcePreservingProductPhotoGenerator.regenerate_role` (신규) | `asset_mode`가 `generated_scene`/`generated_view`인 사진만 같은 자리에서 재생성. `direction`은 생성 프롬프트에 덧붙는 짧은 지시(최대 200자). 최대 3회 |
| `render_page` | `HtmlDetailPageRenderer.render` | HTML 조립 후 Playwright로 PNG |
| `inspect_render(section_index)` | Qwen VLM (신규) | 섹션 PNG(긴 변 1024px로 축소)에서 글자 넘침, 빈 섹션, 깨진 이미지 확인 |
| `finish(summary, warnings)` | — | 종료. 고치지 못한 문제는 `warnings`로 남김 |

`profile_override` 없이 `run`이 호출되는 경우(로컬 데모 API, 평가 스크립트)는 `_compose_profile`과 `_produce_page`를 차례로 부르므로 초안 모드와 렌더 모드가 이어서 돈다.

### 4.3 자기교정 범위

| 감지 | 교정 | 상한 |
|---|---|---|
| 근거 부족으로 빠질 문구가 있음 | `revise_copy` 후 `check_copy_grounding`으로 재확인 | 2회. 그래도 남으면 기존처럼 제외하고 '확인 필요'에 기록 |
| 생성 컷이 어색함 | 방향을 바꿔 `regenerate_photo` | 3회. 같은 자리를 바꾸기만 하므로 생성 컷 수는 늘지 않음 |
| 생성 컷 때문에 생긴 렌더 결함 | 해당 컷 재생성 후 `render_page` | `render_page` 2회 |
| 문구나 레이아웃 때문에 생긴 렌더 결함 | 고치지 않고 `warnings`로 보고 | 승인된 내용이므로 |
| 누끼 실패 (`FALLBACK`) | 재시도하지 않음 | rembg는 결정적이라 다시 돌려도 결과가 같음 |

## 5. 대화형 편집

### 5.1 엔드포인트

```text
POST /internal/v1/ai/detail-page-jobs/{job_id}/draft/edits
헤더  X-AI-Internal-Token
요청  { "instruction": str (1~1000자), "version": int }      ← PUT …/draft와 같은 이름
응답  200 {
        "draft": AiFeDraftResultDto,       // PUT …/draft 응답과 같은 형태
        "reply": str,                       // 장인에게 보여줄 한국어 답변
        "changes": [{ "target": str, "before": str|null, "after": str|null, "evidence": str|null }],
        "warnings": [str]
      }
```

| 코드 | 뜻 |
|---|---|
| `400` | `AGENT_MODE=pipeline`이라 대화형 편집이 꺼져 있음 |
| `401` | 내부 토큰 불일치 |
| `404` | 작업이 없음 |
| `409` | `version` 충돌, 초안이 아직 없음, 승인 중이거나 완료된 초안 (기존 `PUT …/draft`와 같은 판정) |
| `422` | 요청 본문 검증 실패 (지시가 비었거나 1000자 초과). FastAPI 기본 동작 |
| `503` | GPU 잠금을 10초 안에 얻지 못함 (재시도 가능), 모델 서버를 쓸 수 없음 |

- 응답은 동기로 보낸다. 텍스트 전용 Qwen 호출 몇 번이면 끝난다.
- 결과 저장은 기존 `DetailPageJobService.save_draft(expected_version=...)`를 그대로 쓴다. 그래서 `build_editable_draft`와 sanitize를 거친다.
- 실제로 바뀐 것이 없으면 저장하지 않으므로 `version`도 오르지 않는다.
- 대화 기록은 남기지 않는다. 요청마다 "현재 초안 + 지시 한 줄"로 처리한다. 되돌리기는 FE가 이전 버전을 `PUT …/draft`로 다시 저장하면 된다.
- 편집 후 PNG는 만들지 않는다. FE는 `react_document`로 그린다.

### 5.2 편집 모드 도구

| 도구 | 하는 일 |
|---|---|
| `update_copy(path, text, evidence)` | 문구 수정. `path`는 `product_name`, `summary`, `hero_headline`, `hero_description`, `usage_scene`, `blocks.<section_id>.<eyebrow\|title\|body>` 중 하나. `evidence` 필수 |
| `update_block(section_id, variant)` | 블록 변형 변경 (기존 허용 목록 8종) |
| `move_block(section_id, position)` / `remove_block(section_id)` / `add_block(block_type, after_section_id, title, body, evidence)` | 블록 순서 변경, 삭제, 추가 (기존 허용 목록 13종). `hero`와 `closing`은 옮기거나 지우거나 새로 넣을 수 없음 |
| `set_layout(layout_id)` | `editorial-split` / `image-first` / `catalog-grid` |
| `inspect_photo(index, question)` | 새 문구의 근거를 사진에서 확인 |
| `finish(reply)` | 종료 |

편집할 때마다 `ApprovedDraftDto` 검증과 `react_document` 조립을 다시 해서, 저장할 수 없는 초안이 되면 그 자리에서 도구 오류로 돌려준다.

### 5.3 근거 세탁 방지

기존 수동 편집 경로(`ApprovedDraftDto.to_profile`)는 장인이 고친 문구를 모두 `evidence="image-visible"`로 표시한다. 에이전트가 쓴 문장이 이 경로를 그대로 타면, 모델의 추정이 "장인이 확인한 문구"로 둔갑한다. 그래서 `update_copy`의 `evidence`를 다음과 같이 처리한다.

| `evidence` | 뜻 | 처리 |
|---|---|---|
| `creator-instruction` | 장인이 지시에서 직접 말한 내용 | 수동 편집과 같은 취급 |
| `rephrase` | 기존 문구를 줄이거나 다듬기만 함 | 통과. `changes`에 전후를 모두 보여줌 |
| `image-visible` / `user-hints` | 사진이나 입력 정보에 근거 | 통과 |
| `inferred` | 근거 없는 추정 | 반영하지 않고 `warnings`로 장인에게 알림 |

`rephrase`가 새 주장을 넣지 않았는지는 코드로 완전히 검증할 수 없다. 그래서 바뀐 문장 전후를 `changes`로 빠짐없이 돌려주고, 최종 판단은 장인의 승인 단계에 맡긴다. 승인 전에는 아무것도 렌더되지 않는다.

## 6. 가드레일과 오류 처리

### 6.1 코드로 강제하는 불변식

프롬프트에 의존하지 않는다. `user_hints`와 편집 지시는 신뢰할 수 없는 입력으로 취급한다.

| 불변식 | 강제 위치 |
|---|---|
| 모드에 없는 도구는 실행되지 않음 | 모드별 레지스트리. 목록에 없는 이름이면 오류 결과를 돌려줌 |
| 렌더 모드에서 승인 draft는 바뀌지 않음 | frozen `approved_draft`. 렌더 도구에는 문구나 블록 인자가 없음 |
| 원본 사진을 다시 그리지 않음 | `regenerate_photo`는 생성 컷 역할만 받음. 최종 사진 세트는 항상 `ProductFidelityValidator`를 통과해야 함 (기존 `_validated_photo_set`) |
| 생성 컷 수 상한 | 생성 컷은 `prepare_photos`(기존 `MAX_GENERATED_PHOTOS` 적용)에서만 늘어남. `regenerate_photo`는 같은 자리를 바꾸기만 함 |
| 근거 없는 문구는 고객용 결과에 나가지 않음 | 루프가 끝난 뒤 `sanitize_profile_for_render`를 항상 실행 |
| 화면 구조는 서버가 결정 | `react_document`는 기존 빌더와 DTO 검증으로만 만듦 |
| 전달 보장 | outbox, 재전송, lease는 루프 밖 기존 코드가 처리 |
| 한 번에 작업 하나 | GPU 잠금(`_one_job_at_a_time`)을 에이전트 실행 전체 동안 잡음. 편집 모드도 같은 잠금을 씀 |

### 6.2 루프 안의 실패

| 상황 | 처리 |
|---|---|
| 도구 인자가 스키마에 맞지 않음 | 검증 오류를 도구 결과로 돌려줌. 예산 1회 차감 |
| 도구 실행 중 예외 | 예외 종류와 짧은 메시지를 결과로 돌려줌. 같은 도구가 연속 3회 실패하면 폴백 |
| `tool_calls` 없이 텍스트만 반환 | 한 번 재요청. 또 그러면 폴백 |
| `tool_calls` 인자 JSON이 깨짐 | `_parse_json_object`로 복구 시도. 실패하면 인자 오류와 같이 처리 |
| 모델 서버 다운이나 타임아웃 (`LocalModelError`) | 폴백하지 않음. 기존처럼 `FAILED`, `retryable=true` |
| 예산이나 제한 시간 초과 | 폴백 |

### 6.3 폴백

- **완료 조건**
  - 초안: profile 있음, `page_plan` 확정
  - 렌더: 검증된 사진 세트 있음, 렌더 결과 있음
  - 편집: 폴백 없음. 실패하면 변경 없이 `reply`에 실패 사유를 담아 `200`
- 폴백은 `AgentState`를 보고 **빠진 단계만** 기존 `DetailPagePipeline` 순서로 실행한다.
- 폴백이 일어나면 로그와 지표 `agent_fallback_total{mode,reason}`에 남긴다.

### 6.4 관측

- 도구 호출마다 구조화 로그 한 줄: `job_id`, `mode`, `tool`, 인자 요약, 결과 요약, `elapsed_ms`
- `/metrics`
  - `agent_tool_calls_total{mode,tool,outcome}`
  - `agent_tool_seconds{mode,tool}`
  - `agent_fallback_total{mode,reason}`
- trace(도구 호출 기록)는 `AgentState.trace`에 담는다. 평가 스크립트가 꺼내 쓴다. DB 테이블은 만들지 않는다.

## 7. 테스트와 평가

### 7.1 자동 테스트 (외부 모델 호출 없음)

| 대상 | 방법 |
|---|---|
| 루프 | 미리 정한 응답을 순서대로 돌려주는 가짜 채팅 클라이언트로 예산 초과, 모르는 도구, 잘못된 인자, 텍스트만 반환, 연속 실패를 검사 |
| 불변식 | 렌더 모드 draft 불변, `regenerate_photo`의 원본 역할 거부, 생성 컷 상한, 루프가 끝나면 sanitize 실행 |
| 근거 세탁 | 편집 모드에서 `evidence="inferred"` 문구가 `copy_sections`에 들어가지 않음 |
| 폴백 동등성 | 첫 호출부터 폴백하는 클라이언트로 돌린 결과가 `DetailPagePipeline` 결과와 같음 |
| 회귀 | 기존 테스트 전체가 `AGENT_MODE=pipeline`에서 통과 |
| 편집 API | 응답 형태, `409`, `503` |
| 클라이언트 | `chat_with_tools`의 요청 본문(`tools`, `tool_choice`)과 `tool_calls` 파싱 |

### 7.2 실모델 평가

- `scripts/run_eval_pilot.py`(분류별 1점, 6점)를 `pipeline`과 `agent` 양쪽으로 돌린다.
- 자동 지표: 완주율, 폴백률, 건당 소요 시간, 도구 호출 수, sanitize로 빠진 문구 수, 원본 보존 게이트 통과율
- 사람 검수: `scripts/build_review_sheet.py` 4축 검수 시트
- 채택 기준: 사람이 확인하기 전에는 기본값을 `agent`로 확정하지 않는다. 폴백률이 높으면 그 결과를 그대로 문서에 기록한다.

## 8. 구현 단계

각 단계는 브랜치와 PR로 `main`에 합친다.

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| 0 | MLX Serve와 SGLang의 tool calling 실제 동작 확인 | 두 서버에서 `tool_calls` 응답 형태를 기록. MLX Serve가 지원하지 않으면 로컬은 `AGENT_MODE=pipeline`으로 두고 문서화 |
| 1 | `chat_with_tools`, `loop.py`, `AgentState`, 가짜 클라이언트 테스트 | 루프 테스트 통과 |
| 2 | 초안 모드 도구 + 폴백 | 초안 불변식, 폴백 동등성 테스트 통과 |
| 3 | 렌더 모드 도구 + `regenerate_role` | 렌더 불변식 테스트 통과 |
| 4 | `AGENT_MODE` 연결, 지표, `--tool-call-parser` | 기존 테스트 전체 통과 |
| 5 | 편집 모드 + 엔드포인트 | 편집 API, 근거 세탁 테스트 통과 |
| 6 | 실모델 평가, README와 `docs/common/system-architecture.md` 갱신 | 평가 결과 문서화 |

## 9. 알려진 위험

| 위험 | 대응 |
|---|---|
| 27B 4bit 모델의 tool calling 신뢰도 | 예산과 폴백. 폴백률을 지표로 측정 |
| 건당 시간 증가 (현재 약 227초에 Qwen 호출 9~18회 추가) | `inspect_*` 상한. 평가에서 실측 후 예산 조정 |
| MLX Serve의 `tools` 미지원 가능성 | 0단계에서 확인 |
| `rephrase`로 새 주장이 들어갈 가능성 | `changes` 전체 노출 + 장인 승인 |
| 편집 요청이 렌더 작업 뒤에서 대기 | 10초 안에 잠금을 얻지 못하면 `503` |
