# 에이전트 tool calling 스모크 결과

`scripts/runtime/smoke_tool_calling.py`로 텍스트 서버가 OpenAI 형식 `tool_calls`를 돌려주는지 확인한 기록입니다.
지원하지 않는 서버에서는 에이전트 루프가 매번 멈추고(`tool_calls`가 없으면 `no_tool_call`, `tools` 요청을 거부하면 `model_error`) 기존 파이프라인으로 폴백합니다. 결과는 같지만 모델 호출이 한두 번 늘어납니다.

| 날짜 | 서버 | 모델 | 결과 | 비고 |
|---|---|---|---|---|
| 2026-10-14 | MLX Serve 26.9.6 (로컬, M-series 64GB) | ddalcu/Qwen3.8-27B-MLX-Serve-4bit | OK | 서버가 `tool_use` capability를 광고하고 `record_color({"color":"하늘색"})`를 정확히 호출 (2.6초) |
| — | SGLang (Stage) | qwen-text | 미실행 | Task 7의 `--tool-call-parser` 배포 후 확인 |

FAIL이면 로컬 `.env`에 `AGENT_MODE=pipeline`을 둡니다.

## 로컬 실모델 확인 (2026-10-14, MLX Serve)

조건: Qwen3.8-27B 4bit 하나만 띄움. FLUX는 이 Mac에 없어 `--image-provider none`(AI 생성 사진 없음)으로 돌렸습니다. 그래서 `inspect_image`와 `regenerate_photo`는 이번 확인에 포함되지 않았습니다.

### 실서버에서 드러나 고친 점

| 증상 | 원인 | 조치 |
|---|---|---|
| `revise_copy`가 2회 연속 검증 오류 | Qwen이 응답 JSON에서 `evidence`를 빼먹음 | 근거 표시가 없으면 `inferred`로 처리. 정리 단계에서 고객용 문구에서 빠짐 |
| `inspect_render`를 10번 호출하고 8번 거절됨 | 한 턴에 섹션 8개를 한꺼번에 요청. 횟수 제한을 거절된 뒤에야 알게 됨 | 횟수 제한을 도구 설명에 미리 적고, 다 쓴 도구는 다음 턴부터 목록에서 뺌. 렌더 호출 14회 → 8회 |
| 사진 1장인데 `inspect_photo(1)` 호출 | 프롬프트가 추가 사진이 있는 것처럼 읽힘 | 사진이 1장이면 "추가 사진은 없다"고 명시 |
| `HTTP Error 400: Bad Request`만 보이고 이유가 없음 | 서버가 응답 본문에 적은 거절 이유를 클라이언트가 버림 | 오류 메시지에 응답 본문 앞부분을 포함 |

### 파일럿 6건 (분류별 1건, `scripts/run_eval_pilot.py`)

| 사례 | 분류 | 파이프라인 | 에이전트 | 에이전트 종료 사유 (초안 / 렌더) |
|---|---|---|---|---|
| cma-102980 | textile | 118.2초 | 160.1초 | finished(6) / finished(8) |
| cma-101636 | box | 145.0초 | 164.7초 | finished(6) / model_error → 폴백 |
| cma-114971 | metalware | 159.7초 | 156.6초 | model_error → 폴백 / model_error → 폴백 |
| cma-122443 | ceramic | 118.8초 | **실패** (400) | 재실행 시 138.1초, finished(4) / finished(9) |
| cma-109609 | jewelry | 135.5초 | 140.0초 | model_error → 폴백 / model_error → 폴백 |
| cma-110793 | furniture | 102.9초 | **실패** (400) | 재실행 시 113.7초, finished(4) / model_error → 폴백 |

- 완주: 파이프라인 6/6 (평균 130.0초). 에이전트 첫 실행 4/6 (성공 건 평균 155.4초), 실패한 2건은 재실행에서 모두 성공.
- 사람 검수는 아직 하지 않았습니다.

### 400 오류의 원인: MLX Serve 메모리 승인 거절

- MLX Serve는 요청마다 필요한 작업 메모리(이번 경우 약 1.7~2.3GB)를 계산합니다. 그 시점에 확보할 수 있는 양이 모자라면 prefill 전에 거절하고 400을 돌려줍니다. 서버 로그 `~/.mlx-serve/logs/mlx-serve-11234.log`에 `verdict=refuse`, `refused before prefill`, `PrefillDoesNotFit`로 남습니다.
- 확보 가능한 양은 0.7~2GB 사이에서 계속 바뀌었습니다. 같은 사례도 시점에 따라 통과하거나 거절됐습니다.
- 에이전트 모드는 요청 수가 많고 여러 턴 대화를 서버 쪽 prefix 캐시(기본 2GB 예산)에 쌓습니다. 그래서 같은 서버 상태에서 거절되는 시점에 걸리는 일이 더 잦았습니다.
- 모델 오류 폴백은 설계대로 동작해 4건을 살렸습니다. 다만 폴백의 분석 요청까지 거절되면 작업이 실패합니다. 이건 파이프라인 모드도 같습니다.
- **아직 검증하지 않은 완화책**: MLX Serve를 `--prefix-cache-mem 512MB`로 띄워 캐시 예산을 줄이거나, `--os-reserve-gib`(기본: RAM의 1/8)를 낮추는 것. 적용하면 위 파일럿을 다시 돌려 거절 횟수를 비교합니다.

## 이미지 경로 확인: Qwen-Image 2.1 (2026-10-14)

FLUX.2 klein은 이 Mac에서 지워진 상태라 `ddalcu/Qwen-Image-2.1-MLX-Serve-8bit`로 확인했습니다.

- **생성만 됩니다.** MLX Serve가 편집 모드(`mode: "edit"`)를 "requires a FLUX.2 or Mage-Flow-Edit model"로 거절합니다. 그래서 원본을 참조하는 AI 장면·디테일 컷(`generated_scene`/`generated_view`)과 `regenerate_photo`는 이 모델로 확인할 수 없습니다. 코드는 생성 실패로 기록하고 합성 사진으로 대체합니다.
- **단계 수는 20이어야 합니다.** 기본 4단계(FLUX klein용)로는 흐릿한 그라데이션만 나옵니다. 20단계는 1장에 약 37초 걸리고, 합성용 배경으로 쓸 만합니다. 평가 스크립트에 `--image-steps`를 추가했습니다.
- **Qwen3.8-27B와는 함께 올라가지 않습니다.** 텍스트 모델이 올라가 있으면 서버가 "needs ~24.8 GB free … only 9.2 GB is available"(`InsufficientMemory`)로 이미지 모델을 거부하고, 배경은 매번 단색으로 대체됩니다.

### 메모리 설정

- **`--os-reserve-gib 2`로 앞의 400 오류가 사라졌습니다.** 기본값은 RAM의 1/8(8GB)입니다. 2로 띄운 뒤 같은 파일럿에서 거절이 0건이었습니다. macOS는 비활성 메모리를 많이 쥐고 있어 그 순간의 여유 메모리가 작게 잡히는데, 거기서 8GB를 또 빼서 모자랐던 것입니다.
- `--prefix-cache-mem 512MB`: 여러 턴 대화 캐시를 줄여 이미지 모델이 쓸 몫을 남깁니다.

### 텍스트 모델별 결과 (에이전트 모드, 파일럿 6건, Qwen-Image 20단계)

| 텍스트 모델 | 완주 | 평균 소요 | 생성 배경 | 비고 |
|---|---|---|---|---|
| Qwen3.8-27B 4bit (16.9GB) | 6/6 | 약 171초 | **0장** (이미지 모델 로드 거부) | 렌더 2건이 거절된 호출로 예산 소진 → 이후 수정 |
| Qwen3.5-4B 4bit (2.9GB) | 5/6 | 약 96초 | 6장 | 도자기 1건이 분석 결과 형식 불일치로 실패. 5건 모두 상품명(`display_name`) 비어 있음. 가구를 '악기'로 판정. 문구가 "제품의 사용 상황을 확인하실 수 있습니다"처럼 일반적. 초안에서 `revise_copy`·`inspect_photo`를 쓰지 않음 |

- Qwen3.5-4B는 섹션 이름을 사진 ID로 착각해 `inspect_image`가 3번 연속 실패했습니다. 이제 오류 결과에 쓸 수 있는 사진 ID 목록을 함께 돌려줍니다.
- 결론: 로컬에서 Qwen-Image와 함께 쓰려면 4B급이 필요하고, 그만큼 문구와 분류 품질이 떨어집니다. 운영과 같은 품질을 확인하려면 FLUX.2 klein 9B(8.9GB, Qwen3.8과 합 25.8GB)를 다시 받는 쪽이 맞습니다. 사람 검수는 아직 하지 않았습니다.

