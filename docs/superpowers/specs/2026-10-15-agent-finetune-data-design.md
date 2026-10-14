# 전용 에이전트 파인튜닝 데이터 구축 설계

- 작성일: 2026-10-15
- 목적: **개인 학습**. 이 에이전트를 로컬의 작은 비전 모델(Qwen3.8-9B-Distill 4bit, Qwen3.5-4B)로 돌릴 때 생기는 문제를 파인튜닝으로 줄이는 것
- 기준 브랜치: `feat/single-agent` (PR #1) 위의 `feat/agent-finetune-data`

## 1. 왜 이 데이터인가

로컬 MLX 실측(`docs/internal/agent-tool-calling-smoke.md`)에서 작은 모델이 실패한 지점은 다음 세 가지였습니다.

| 문제 | 관찰 | 필요한 학습 신호 |
|---|---|---|
| 분석 결과 형식 위반 | 9B: 6건 중 2건 `ProductProfileDto` 불일치 또는 JSON 깨짐. 4B: 1건 | 사진 + 실제 분석 프롬프트 → 올바른 `ProductProfileDto` JSON |
| 도구 규칙 위반 | 횟수가 다 찬 `inspect_render` 반복 호출, 섹션 이름을 사진 ID로 사용, `evidence` 누락 | 이 에이전트의 도구 정의로 진행된 깨끗한 다중 턴 대화 |
| 빈약한 한국어 공예 문구 | "제품의 사용 상황을 확인하실 수 있습니다", 상품명 누락 | 실제 공예 판매자가 쓴 한국어 문구와 상품 정보 |

그래서 **실제 공예 상품 페이지를 수집**해 내용(사진, 상품 정보, 판매 문구)을 얻고, 그것을 **이 에이전트의 입출력 형식**으로 바꿉니다. 교사 모델이 내용을 새로 쓰는 증류는 쓰지 않습니다.

## 2. 출처

| 출처 | 접근 | 판단 |
|---|---|---|
| 공예정원 `kcdfshop.kr` (KCDF 운영 공예 상품몰, 전체 1,027개) | robots.txt가 상품 페이지 허용 (관리·회원·API 경로만 차단) | **사용**. 저작권은 판매 작가에게 있으므로 로컬 보관, 재배포 금지 |
| 아이디어스, 네이버 스마트스토어 | robots.txt가 일반 수집기 전체 차단 | 사용 안 함 |
| e뮤지엄 | 웹은 GPTBot 차단, 공식 오픈 API는 공공누리 1유형 | 이번 범위 밖. 필요하면 오픈 API로 별도 수집 |

## 3. 흐름

```text
① 크롤링   공예정원 전체상품 목록 → 상품 페이지
           robots.txt 준수, 2초에 1회, 목적이 드러나는 User-Agent, 이어받기, 원본 HTML 캐시
           → 상품명, 분류, 대표 사진, 상품상세정보 표(소재·규격·구성·용도·인증·제조), 유의사항, 상세 설명 이미지
② OCR      상세 설명 이미지 → macOS Vision OCR(ko-KR, en-US, 정확도 우선)
           긴 이미지는 높이 2,000px, 겹침 200px로 잘라 읽고 위→아래로 합치며 겹친 줄 제거
③ 정답     규칙: 상품정보 표 → info_table 블록과 장인 입력, 유의사항 → notice 블록과 care_guide,
                 분류·재질 → product_type·keywords
           27B 정리: OCR 문구를 copy_sections와 features 칸에 나누기만 함. 원문에 없는 문장은 버림
④ 예제     분석: 사진 + 실제 LocalProductAnalyzer 프롬프트 → 정답 ProductProfileDto JSON
           대화: 정답으로 실제 DetailPageAgent를 각본 클라이언트로 실행해 모델에 전달된 메시지를 기록
                 초안, 렌더, 편집(지시 템플릿 2개)
⑤ 내보내기 JSONL {id, kind, product_id, category, split, images, messages, tools}
           상품 ID 해시로 분류별 train/val 9:1
```

## 4. 규칙

### 4.1 사용할 상품
- 대표 사진을 열 수 있어야 함
- 상품정보 표에 `소재` 또는 `용도`가 있어야 함
- OCR 한글이 80자 이상이어야 함

### 4.2 장인 입력(`user_hints`) 매핑
| 필드 | 출처 | 길이 제한 |
|---|---|---|
| `product_name` | 상품명 | 120자 |
| `care_guide` | 유의사항 텍스트 | 1,000자 |
| `making_method` | OCR 문장 중 제작 관련 표현(제작, 공정, 손으로, 수작업, 빚, 굽, 가마, 옻칠, 자개, 짜, 엮, 염색, 바느질 등)이 들어간 문장 | 2,000자 |

### 4.3 27B 정리 검증
- 출력한 각 `title`과 `description`은 공백과 문장부호를 뺀 뒤 OCR 원문의 부분 문자열이거나, 원문 구간과의 유사도(difflib)가 0.9 이상이어야 합니다. 아니면 그 항목을 버립니다.
- 통과한 `copy_sections`가 2개 미만이면, 그 상품은 분석 예제에서 빼고 규칙만으로 만든 정답으로 대화 예제에만 씁니다.
- 판매자가 제공한 사실이므로 `evidence="image-visible"`로 둡니다. 근거 표시는 지금 분석 프롬프트 관례를 따릅니다.
- 최종 정답은 `ProductProfileDto` 검증, `validate_product_profile`, `ensure_editorial_page_plan`, `sanitize_profile_for_render`를 통과해야 합니다.

### 4.4 각본 대화
| 모드 | 도구 순서 |
|---|---|
| 초안 | `analyze_photos` → `check_copy_grounding` → `plan_page` → `finish` |
| 렌더 | `prepare_photos` → `verify_photos` → `render_page` → `finish` |
| 편집 | 지시 템플릿에서 2개 선택 |

편집 지시 템플릿은 세 종류입니다.
- 구조 변경: 배경 변형, 레이아웃, 블록 순서, 블록 삭제
- 유의사항 블록 추가: `evidence="user-hints"`
- 근거 없는 주장 요구(예: "100년 된 장인의 작품이라고 써줘"): `evidence="inferred"`로 반영하지 않고 경고

`analyze_photos`는 정답 프로필을 돌려주는 분석기로 실행합니다. 도구 결과와 프롬프트는 실제 실행에서 나온 것을 그대로 기록합니다.

## 5. 보관과 권리
- 원자료와 결과물: `.local/finetune/kcdf/` (git 제외). 재배포하지 않습니다.
- 레포에 커밋하는 것: 수집기, OCR, 변환기, 테스트(합성 HTML 픽스처만), 데이터셋 카드(개수, 기준, 출처, 날짜)

## 6. 검증
- 단위 테스트: 파싱, robots 준수와 간격, OCR 타일 합치기, 원문 일치 검증, 정답 프로필 검증, 각본 대화의 도구 순서와 `inferred` 미반영, 나누기의 결정성과 상품 단위 분리
- mlx-vlm 학습 로더가 내보낸 JSONL을 실제로 읽는지 확인
- Codex(`codex exec -s read-only`) 교차 검토 두 번: 이 설계(선정 기준), 그리고 결과 샘플·데이터셋 카드

## 7. 구현 순서
1. 크롤러 + 테스트 → 백그라운드 크롤링 시작
2. OCR + 테스트 → 수집된 상품부터 OCR
3. 정답 빌더(규칙 + 27B 정리 + 검증) + 테스트 → 백그라운드 정리
4. 각본 대화 생성기 + 테스트
5. 내보내기, 데이터셋 카드, mlx-vlm 로더 확인
6. Codex 결과 검토 → 반영

## 8. Codex 검토 1 반영 (2026-10-15)

검토 원문: `docs/internal/finetune-data-codex-review-1.md`

| 지적 | 반영 |
|---|---|
| 판매자 문구를 `image-visible`로 표시하면 사진에 안 보이는 사실까지 시각 근거로 가르친다 | 정답의 모든 문장을 **모델이 받는 입력 텍스트**(`user_hints`)로 다시 검증한다. 입력에 없는 문장은 버린다. 현재 스키마에 "장인 제공" 근거 값이 없어 분석 프롬프트 관례대로 `image-visible`을 쓰되, 모든 표시가 입력 텍스트로 뒷받침된다. `creator-provided` 근거 값 추가는 서비스 동작 변경이라 별도 제안으로 남긴다 |
| 상품정보 표가 모델 입력에 없는데 `info_table` 정답에 들어간다 | 장인이 "제작 과정" 칸에 판매 문구와 상품 정보를 붙여 넣은 상황으로 보고, `making_method` = 정리한 OCR 문구 + 상품정보 표 줄(2,000자 이내, 줄 단위로 자름)로 둔다. `info_table`과 `scale_reference`는 입력에 남은 표 줄만 쓴다 |
| `revise_copy`, 오류 회복, 거절 경로가 없다 | 초안 대화 변형 추가. ① `revise_copy`: 특징 하나를 근거 미확인으로 둔 분석 결과에서 시작해, 판매자 원문으로 다시 확인. 내부 `revise_copy` JSON 호출도 별도 예제로 내보냄. ② 오류 회복: 분석 전에 `check_copy_grounding` 호출 → 실제 도구 오류 → `analyze_photos`. 잘못된 `layout_id` 인자 → 실제 검증 오류 → 올바른 인자로 재호출. ③ 편집: 지시(`creator-instruction`), 다듬기(긴 설명의 첫 문장만 남기기, `rephrase`), 장인 입력(`user-hints`), 근거 없는 주장 거절(`inferred`) |
| `inspect_photo`, `inspect_render`가 없다 | 정답 판단(사진이나 렌더에서 무엇이 보이는지)을 모델 없이 만들 수 없어 이번 범위에서 뺀다. 사진이 1장인 상품은 `inspect_photo`를 부르지 않는 경로만 담긴다. 한계로 데이터셋 카드에 적는다 |
| OpenAI와 mlx-vlm 형식을 한 파일로 겸용하지 마라. 이미지 과제와 도구 대화를 분리하라 | 세 가지 파일로 내보낸다. `vlm/{train,val}.jsonl`(분석·`revise_copy` 내부 호출, mlx-vlm 형식 `images` + `{"type":"image"}` 콘텐츠), `agent/{train,val}.jsonl`(텍스트 전용 `messages` + `tools`), `meta.jsonl`(id·kind·상품·묶음·분류·split·출처). 대상 모델 채팅 템플릿으로 렌더링되는지 확인한다 |
| 같은 작가·시리즈가 train과 val에 나뉘면 누수 | 정리 후 같은 문구 이미지를 공유하는 상품을 한 묶음으로 보고 묶음 단위로 나눈다. 공통 배너처럼 정리 후 비는 이미지는 묶음 판단에서 뺀다 |
| 대화 중 도구 목록이 바뀔 수 있다 | 각본 대화는 횟수 상한에 닿지 않아 목록이 바뀌지 않는다. 마지막 요청의 도구 목록을 저장한다 |

## 9. Codex 검토 2 반영 (2026-10-15)

검토 원문: `docs/internal/finetune-data-codex-review-2.md`. 사용자 결정: `creator-provided` 근거 값 추가, 27B는 이미지 판정에만 사용.

| 지적 | 반영 |
|---|---|
| 판매자 문구가 `image-visible`로 표시된다(두 번째 지적) | 서비스에 근거 값 `creator-provided`를 추가했다(PR #2). `sanitize`는 설명 문장이 `user_hints`에 실제로 있을 때만 통과시킨다. 정답의 문구·특징·표 항목은 모두 `creator-provided`로 표시한다. 4.3의 `image-visible` 관례는 폐기 |
| OCR 오타("곁을" → "고측을")가 유사도 0.9로 통과 | 퍼지 일치를 없앴다. 공백·문장부호를 뺀 정확한 부분 일치만 허용 |
| 유의사항에 탭 이름(상품리뷰, 상품문의)과 HTML 주석이 섞임 | 주석을 지우고, 상품정보 표 앞의 마지막 "유의사항" 제목 뒤만 읽는다. 저장한 HTML을 빌드 때 다시 파싱해 재수집 없이 반영. 법률·고객센터 문장(품질보증, 소비자분쟁, A/S, 교환·반품·배송)도 뺀다 |
| revise 예제가 판매자 문구를 시각 근거로 다시 라벨링한다 | revise 정답은 `creator-provided`로 표시한다. 근거 미확인 → 장인 입력으로 확인이라는 실제 과제가 된다 |
| 렌더 대화, `inspect_*` 호출, 사진 ID 오류 회복이 없다 | 27B를 이미지 판정에만 쓴다. 렌더 대화(작가 묶음당 1개): 섹션 이름을 사진 ID로 쓴 `inspect_image` → 실제 도구 오류(있는 사진 목록) → 올바른 ID로 재호출 → 렌더 → `inspect_render` 2회(상한) → `finish`. 초안의 약 30%는 추가 사진에 `inspect_photo`를 부른다. 판정 답은 `inspect` 이미지 과제로도 내보낸다. 형식이 틀렸거나 인용한 글자가 장인 입력과 다른 판정은 버리고 판정 없는 대화로 다시 만든다 |
| `finish` 요약이 개수 템플릿이다 | 도구 결과에서 요약을 만든다. 블록 구성, 확인한 문구 수, revise·추가 사진 확인 여부, 렌더의 검증 사진 수·섹션 수·검수 결과와 경고 |
| val이 한 시리즈에 몰리고 작가 단위로 묶이지 않는다 | 상품 페이지 JSON-LD의 `brand.name`(입점 작가)을 읽어 같은 작가 상품을 한 묶음으로 둔다. `meta.jsonl`에 `artist`를 기록한다 |

