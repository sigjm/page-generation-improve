# Codex 교차 검토 2: 결과 샘플과 데이터셋 카드 (2026-10-15)

`codex exec -s read-only`로 받은 검토 원문입니다. 질문은 설계 문서 6장의 두 번째 검토(상품 22개로 만든 중간 데이터셋의 실제 행)입니다. 반영 내용은 설계 문서 9장에 있습니다.

## 1. Analysis 예제

다섯 예제의 `copy_sections`, `features`, `page_plan`의 제목·본문·항목, `summary`를 프롬프트의 `<creator-data>`와 대조했습니다. 대부분은 입력 문장을 그대로 쓰거나 입력 단어로 짧은 제목을 만들었습니다. 다섯 예제의 페이지 계획도 후보 원형의 순서와 variant를 지킵니다.

- `kcdf-2365-analysis-0`: `process-evidence-deep`
- `kcdf-2366-analysis-0`: `complete-comparison`
- `kcdf-2390-analysis-0`: `set-lineup-first`
- `kcdf-2402-analysis-0`, `kcdf-2411-analysis-0`: `component-inventory-detail`

다만 `kcdf-2411-analysis-0`에는 입력과 다른 문구가 있습니다. 입력은 **“곁을 은은하게 밝혀주는 호롱으로 완성했습니다.”**인데, `copy_sections[4].title`과 `usage_scene.body`에는 **“고측을 은은하게 밝혀주는 호롱으로 완성했습니다.”**라고 나옵니다. 문자 유사도 검사에서 통과한 것으로 보입니다. 현재 검증은 정확한 일치 외에 0.9 이상 유사도도 허용합니다([`gold.py:94`](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/finetune/gold.py:94)).

또 `kcdf-2390-analysis-0`, `kcdf-2402-analysis-0`의 notice에는 `상품리뷰(0) 상품문의 <!-- 공예정원 | 소비자 안내 &`가 들어갑니다. 입력 블록에 실제로 있지만 관리 안내가 아니라 페이지 잔여 문구입니다.

더 근본적으로, 판매자 문구에서 온 `features`와 상품정보 항목도 `image-visible`로 표시됩니다. 예컨대 `kcdf-2365-analysis-0`의 특징 설명은 입력 문구를 쓰면서 시각 근거로 분류합니다. 정답에는 내용의 근거 출처와 evidence 라벨 사이에 불일치가 남아 있습니다([`gold.py:285`](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/finetune/gold.py:285)).

## 2. Agent 대화

일부 회복 신호는 있습니다. `kcdf-2365-agent_draft-0`은 허용되지 않는 `layout_id` 오류 뒤 `plan_page`를 다시 호출하고, `kcdf-2367-agent_draft-0`은 분석 전 검사 오류 뒤 분석을 수행합니다. 도구 인자도 딕셔너리 형태이며, `update_copy`와 `add_block` 예제에는 필요한 `evidence`가 들어 있습니다. `kcdf-2395-agent_edit_refuse_claim-0`은 `inferred` 주장이 거절된 뒤 거절 이유를 알리는 흐름이 일관됩니다.

하지만 1차 검토의 핵심 도구 실패는 다뤄지지 않았습니다. 현재 `stats.json`에는 `agent_render`가 없고, 대화에도 `inspect_render` 반복 호출이나 사진 ID 오류 회복 예제가 없습니다. draft 예제에는 `inspect_photo` 도구 정의가 있어도 실제 호출은 없습니다. 따라서 도구 반복과 사진 ID 혼동을 바로잡는 학습 신호는 확인되지 않습니다.

대화는 결과를 보고 다음 행동을 고르는 사례라기보다 미리 정한 각본에 가깝습니다. `conversations.py`의 `ScriptClient`는 호출 목록을 먼저 받아 재생합니다([`conversations.py:35`](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/finetune/conversations.py:35)). 대부분의 draft는 `analyze_photos → check_copy_grounding → plan_page → finish` 순서이고, 편집은 한 번 수정한 뒤 finish로 끝납니다. finish 요약도 `“상품명 초안: 문구 N개, 블록 M개”` 같은 개수 템플릿입니다(`kcdf-2365-agent_draft-0`).

나쁜 학습 신호도 있습니다. `kcdf-2366-agent_edit_add_notice-0`, `kcdf-2380-agent_edit_add_notice-0`은 같은 리뷰·문의 잔여 문구를 `관리 방법 안내` 블록에 넣고, 성공 응답까지 기록합니다. `page.notice`가 실제 관리 안내인지 확인하지 않고 그대로 쓰는 흐름입니다([`conversations.py:257`](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/finetune/conversations.py:257)).

## 3. Revise 예제

일곱 revise 출력은 입력에 있는 문장이나 구절을 그대로 사용합니다. 예를 들어 `kcdf-2390-revise-0`의 “한 송이 꽃을 상감으로 새긴 원형 수납함”은 입력에 있고 새 사실을 보태지 않습니다. 근거 보존이 목표라면 원문을 유지하는 선택 자체는 적절할 수 있습니다.

다만 이 예제들은 실질적인 재작성 신호가 아닙니다. 기존 feature의 evidence만 인위적으로 `inferred`로 바꾼 다음 같은 제목·설명을 그대로 반환하고 `image-visible`로 표시합니다([`conversations.py:145`](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/finetune/conversations.py:145)). 즉, 문구를 고치는 법보다 **판매자 문구를 시각 근거로 다시 라벨링하는 법**을 가르칩니다. 수정된 내용은 입력에 근거하지만, evidence 라벨은 잘못됐습니다. revise 품질 사례로 세기에는 부적절합니다([`prompts.py:76`](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/agent/prompts.py:76)).

## 4. 분할

현재 `meta.jsonl`의 9개 group은 각각 train 또는 val 한쪽에만 있습니다. 눈에 띄는 시리즈 누수는 없습니다. `group-2390`의 꽃 시리즈 상품 2390–2395는 모두 val이고, 유리 획 시리즈 `group-2378`은 train입니다([`meta.jsonl:35`](/Users/jmllem/PycharmProjects/page-generation-improve/.local/finetune/kcdf/dataset/meta.jsonl:35)).

다만 val의 5개 analysis 예제는 사실상 `group-2390` 한 시리즈에 모여 있습니다. 현재 val은 상품 수보다 독립적인 작가·시리즈 수가 훨씬 적습니다. 또한 `meta`에는 작가·시리즈 필드가 없고, 그룹 생성은 OCR 텍스트가 있는 **동일 이미지 해시**를 기준으로 합니다([`export.py:10`](/Users/jmllem/PycharmProjects/page-generation-improve/src/detail_page_ai/finetune/export.py:10)). 이미지가 달라도 같은 작가나 시리즈일 때는 자동으로 묶이지 않을 수 있습니다.

## 5. 1,000개로 늘릴 때 커질 문제

- 잘못된 `image-visible` 라벨과 관리 안내 잔여 문구가 반복되면, 근거와 상품 데이터의 구분 오류가 대량으로 학습됩니다.
- 퍼지 일치로 통과한 `고측` 같은 OCR 오류가 늘어날 수 있습니다. 1차 검토의 evidence 출처 문제는 문구를 입력과 대조한 것만으로 해결되지 않았습니다.
- 각본의 고정 순서와 단순 finish 요약이 1,000개로 복제됩니다. 반면 render 검사 반복·사진 ID 회복·evidence 누락 오류의 교정 예제는 현재 없습니다.
- 현재 표본은 모두 `인테리어`이고, val은 한 시리즈에 집중돼 있습니다. 크롤링이 늘어도 이 분포와 분할 방식을 그대로 두면 범용 성능 평가가 어렵습니다.

## 반드시 고칠 것

- 판매자 제공 정보와 시각 근거의 라벨을 분리하거나, 현재처럼 판매자 문구를 `image-visible`로 학습하는 예제를 제외하세요.
- `page.notice`에서 리뷰·문의·HTML 잔여물을 걸러내고, `kcdf-2411-analysis-0`의 `고측` 문구는 수정하거나 제외하세요.
- revise 예제의 인위적 evidence 변경을 바로잡으세요. 반복 호출·사진 ID·evidence 오류를 해결했다고 보려면 해당 오류와 회복을 담은 대화가 필요합니다.

## 고치면 좋은 것

- 작가·시리즈 단위로 묶음을 보강하고, val에 여러 독립 그룹을 확보하세요.
- 결과에 따라 도구를 선택하는 조건부 경로와 finish의 실제 작업 요약을 추가하세요.

## 그대로 둬도 되는 것

- 확인한 다섯 analysis의 `page_plan` 후보 순서와 variant 준수.
- 올바른 도구 인자 형식, invalid `layout_id` 회복, `inferred` 거절 응답.
- 원문을 보존하는 revise 방식은 “수정할 필요가 없으면 그대로 유지”하는 과제로 정의할 때 유효합니다.

파일은 읽기만 했고 수정이나 테스트 실행은 하지 않았습니다.