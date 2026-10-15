# 데이터셋 카드: KCDF 공예 상품 기반 에이전트 파인튜닝 세트

- 목적: **개인 학습·포트폴리오**. 이 서비스의 단일 에이전트를 로컬에서 **Qwen3.8-9B-Distill 4bit(파인튜닝) + Qwen-Image 2.1 4bit**로 돌릴 때 생기는 형식 오류, 도구 규칙 위반, 빈약한 한국어 문구를 줄이는 것
- 최종 구성에 27B는 없습니다. 데이터는 이 구성 그대로(이미지 생성 켜짐, 실제 Qwen-Image 사진) 만듭니다
- 설계: `docs/superpowers/specs/2026-10-15-agent-finetune-data-design.md`
- 교차 검토: `docs/internal/finetune-data-codex-review-1.md`, `docs/internal/finetune-data-codex-review-2.md`
- 위치: `.local/finetune/kcdf/` (git 제외). **재배포 금지**

## 출처와 권리

| 항목 | 내용 |
|---|---|
| 출처 | 공예정원 온라인숍 `https://www.kcdfshop.kr` (한국공예디자인문화진흥원 운영), 전체상품 약 1,016개 |
| 수집 | robots.txt 준수(와일드카드 규칙 포함), 페이지 2초·이미지 1초 간격, 수집 목적을 밝힌 User-Agent |
| 권리 | 상품 사진과 문구는 판매 작가의 저작물. 개인 학습용으로만 로컬에 두고, 원자료·파생 데이터·이 데이터로 학습한 가중치를 공개하지 않는다 |
| 교사 모델 | Codex CLI(`codex exec`, 읽기 전용, 추론 xhigh, Fast 등급). ① OCR 문장을 역할별로 **고르기만** 하고 원문에 없는 문장은 검증에서 버린다 ② `inspect_photo`, `inspect_image`, `inspect_render`의 이미지 질문에 답한다. 데이터를 OpenAI로 보내는 것은 개인 학습·포트폴리오 용도라 사용자가 허용했다 |
| 이미지 모델 | `ddalcu/Qwen-Image-2.1-MLX-Serve-4bit`, 20단계. 렌더 대화의 사진 구성(배경 생성 + 원본 합성)을 실제로 만든다 |

## 파일

```text
.local/finetune/kcdf/
├── raw/<상품ID>/        page.html, record.json, 사진, ocr.json, structured.json
└── dataset/
    ├── images/<상품ID>.jpg   대표 사진(긴 변 1,280px). <상품ID>-judge-N.*은 판정에 쓴 사진·렌더 섹션
    ├── vlm/{train,val}.jsonl  이미지 과제 (mlx-vlm 형식)
    ├── agent/{train,val}.jsonl 도구 호출 대화 (텍스트 전용, messages + tools)
    ├── meta.jsonl            id, kind, 상품, 묶음, 작가, 분류, split, 출처 URL
    └── stats.json            종류·split별 개수, 건너뛴 이유
```

### `vlm/*.jsonl` (이미지 과제)
```json
{"id": "kcdf-2365-analysis-0",
 "images": ["images/2365.jpg"],
 "messages": [
   {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": "<LocalProductAnalyzer가 실제로 보내는 프롬프트>"}]},
   {"role": "assistant", "content": [{"type": "text", "text": "<정답 ProductProfileDto JSON>"}]}]}
```
- `analysis`: 사진 + 실제 분석 프롬프트(레이아웃 후보 4개 포함) → 정답 `ProductProfileDto`
- `revise`: `revise_copy` 도구 안에서 보내는 재작성 프롬프트 → 판매자 원문으로 확인한 JSON(`creator-provided`)
- `inspect`: `inspect_*` 도구 안에서 보내는 검사 프롬프트 + 사진 또는 렌더 섹션 → Codex 판정 JSON(`teacher` 필드에 교사 이름)

### `agent/*.jsonl` (도구 호출 대화)
- `messages`: system, user, assistant(`tool_calls`), tool … 순서의 실제 에이전트 대화. `tool_calls[].function.arguments`는 **딕셔너리**입니다(Qwen 템플릿이 항목을 순회함).
- `tools`: 마지막 요청에 실린 도구 정의
- 종류
  - `agent_draft`: 기본, `revise_copy` 포함(약 40%), 분석 전 검사 오류 회복(10%), 잘못된 `layout_id` 인자 회복(10%), 추가 사진 `inspect_photo`(추가 사진이 있는 상품의 약 30%)
  - `agent_render`: 묶음당 1개. `inspect_image`에 섹션 이름을 넣은 오류 → 있는 사진 목록을 보고 재호출 → `render_page` → `inspect_render` 2회(상한) → `finish`
  - `finish` 요약은 도구 결과(블록 구성, 확인한 문구 수, 렌더 섹션 수, 검수 경고)로 만듭니다
  - `agent_edit_<종류>`: 상품당 2개. 종류는 `dark_closing`, `image_first`, `remove_block`, `add_notice`(근거 `user-hints`), `retitle`(`creator-instruction`), `shorten`(`rephrase`), `refuse_claim`(`inferred` → 반영 안 하고 경고)

## 정답 규칙
- 장인 입력(`user_hints`)
  - `product_name`: 상품명
  - `making_method`: 정리한 OCR 판매 문구 + 상품정보 표 줄(2,000자 이내, 줄 단위로 자름)
  - `care_guide`: 유의사항(탭 이름, HTML 주석, 법률·고객센터·교환·배송 문장 제외)
- 정답의 모든 문장은 **모델이 받는 입력 텍스트**에 그대로 있어야 합니다(공백·문장부호만 무시한 정확한 부분 일치). OCR 오타를 고친 문장도 통과하지 않습니다. 짧은 이름표는 입력에 있는 단어로만 이뤄져야 합니다.
- 판매자 문구에서 온 문구·특징·표 항목의 근거는 `creator-provided`입니다(PR #2에서 추가한 값). 사진에서 본 것처럼 `image-visible`로 표시하지 않습니다.
- `info_table`과 `scale_reference`는 입력에 남은 상품정보 표 줄만 씁니다.
- 레이아웃: 프롬프트가 제시한 후보 4개 중 실제 데이터로 채울 수 있는 블록이 가장 많은 원형을 고르고, 그 순서와 변형을 따릅니다. 근거 없는 블록(`palette`, `recommendation` 등)은 뺍니다.
- 정답은 서비스와 같은 검사(`validate_product_profile`, `ensure_editorial_page_plan`, `sanitize_profile_for_render`, `react_document` 조립)를 모두 통과해야 합니다.
- OCR 정리: 반복 줄, 연출 사진 안내, 고객센터·배송 안내, 쇼핑몰 공통 배너, 연도로 시작하는 작가 이력 줄을 뺍니다.

## 나누기
- 같은 작가(상품 페이지 JSON-LD의 `brand.name`)이거나 정리 후 같은 문구 이미지(작가 소개, 시리즈 설명)를 공유하는 상품을 한 묶음으로 보고, 묶음 해시로 train/val을 9:1로 나눕니다. 작가 단위로 나뉘므로 val은 학습에 없던 작가의 상품입니다.
- 분석 예제는 묶음(작가)당 최대 5개로 제한해 한 작가가 데이터를 독차지하지 않게 합니다.

## 학습 (mlx-vlm 0.7.8)
- **`agent/*.jsonl`을 그대로 넣지 마세요.** mlx-vlm은 `tools` 없이 `messages`만 템플릿에 넣고, 마지막 assistant 턴만 학습합니다. 실측에서 1,168토큰짜리 대화 가운데 학습되는 것은 `finish` 76토큰뿐이었습니다.
- `scripts/dataset/prepare_mlx_vlm_sft.py`가 `mlx-vlm/{train,validation}.jsonl`을 만듭니다. 도구 대화는 assistant 턴마다 한 샘플로 나누고, 템플릿이 그리는 도구 블록을 system 메시지에 넣습니다. 모든 샘플이 "mlx-vlm 렌더링 = 실행 시 렌더링(도구 포함)"인지 확인하고, 다르면 멈춥니다.
- 분석 예제는 약 10,700토큰(이미지 약 1,100토큰)이라 `--max-seq-length 12288`로 잡습니다. `--train-on-completions --grad-checkpoint`를 씁니다.
- 실측(M-series 64GB, LoRA rank 8, batch 1): 분석 행은 MLX 최대 메모리 **43.6GB**, 도구 대화·판정 행만이면 19.4GB입니다. Qwen-Image 4bit(15.5GB)를 올린 MLX Serve와 같이 돌리면 64GB에 거의 꽉 차므로, 학습은 MLX Serve를 내리고 하세요.
- MLX Serve는 LoRA 어댑터를 읽지 못합니다. `scripts/dataset/fuse_lora.py`로 어댑터를 4bit 모델에 병합해 모델 폴더를 만듭니다(크기 5.6GB 그대로).

## 한계
- 출처가 공예 상품몰 한 곳이라 문체와 페이지 형식이 한쪽으로 치우칩니다. 평가셋 `cma_real_v1`(박물관 소장품)과 분포가 다르므로, 그 평가는 시각 근거·추측 억제 확인용 보조 holdout으로만 쓰세요.
- `creator-provided`는 PR #2의 서비스 변경이 전제입니다. PR #2 없이 학습하면 모델이 서비스가 모르는 근거 값을 냅니다.
- 이미지 판정은 Codex의 답이라 증류입니다. 인용 글자가 장인 입력과 다른 판정은 버리지만, 인용 없이 틀린 판정은 걸러지지 않습니다.
- MLX Serve의 Qwen-Image 2.1은 편집 모드를 지원하지 않습니다. 원본을 참조하는 AI 사용 장면·디테일 컷은 항상 실패하고 합성 사진으로 대체되며, 데이터에도 그대로 담깁니다.
- `regenerate_photo`는 담지 않았습니다(원본 보존 사진은 다시 만들 수 없음).
- 대화는 각본이라 순서 다양성이 제한됩니다(초안 변형 5종, 렌더 1종, 편집 7종).

## 다시 만들기
```bash
uv pip install -p .venv/bin/python -r pyproject.toml --extra dev --extra dataset
PYTHONPATH=src .venv/bin/python scripts/dataset/build_kcdf_agent_finetune.py crawl
PYTHONPATH=src .venv/bin/python scripts/dataset/build_kcdf_agent_finetune.py ocr
PYTHONPATH=src .venv/bin/python scripts/dataset/build_kcdf_agent_finetune.py structure   # Codex, --workers 8
# Qwen-Image 4bit: mlx-serve pull은 하위 폴더를 받지 않으므로 hf download로 받고,
# 서버는 --skip-mem-preflight로 띄운다(비활성 메모리를 빼고 계산해 로드를 거절함)
PYTHONPATH=src .venv/bin/python scripts/dataset/build_kcdf_agent_finetune.py build --workers 8

# 학습 환경: uv venv -p 3.13 .local/venvs/mlx-vlm && uv pip install -p .local/venvs/mlx-vlm/bin/python mlx-vlm==0.7.8 torch torchvision
PYTHONPATH=src .local/venvs/mlx-vlm/bin/python scripts/dataset/prepare_mlx_vlm_sft.py --model ~/.mlx-serve/models/empero-ai/Qwen3.8-9B-Distill-MLX-4bit
.local/venvs/mlx-vlm/bin/python -m mlx_vlm.train --model ~/.mlx-serve/models/empero-ai/Qwen3.8-9B-Distill-MLX-4bit \
  --data .local/finetune/kcdf/dataset/mlx-vlm --train-on-completions --grad-checkpoint --max-seq-length 12288 \
  --output-path .local/finetune/lora/run-1
.local/venvs/mlx-vlm/bin/python scripts/dataset/fuse_lora.py --model ~/.mlx-serve/models/empero-ai/Qwen3.8-9B-Distill-MLX-4bit \
  --adapter .local/finetune/lora/run-1 --out ~/.mlx-serve/models/local/Qwen3.8-9B-kcdf-agent-4bit
```
크롤링·OCR·정리는 끝난 상품을 건너뛰고, 빌드는 상품별 대화를 `dataset/cache/`에 저장하므로 중간에 멈춰도 다시 실행하면 이어집니다. 대화 생성 규칙을 바꾼 뒤에는 `dataset/cache/`를 지우고 다시 빌드하세요.
