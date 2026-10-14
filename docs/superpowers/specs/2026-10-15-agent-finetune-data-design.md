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
