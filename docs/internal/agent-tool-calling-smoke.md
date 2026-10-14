# 에이전트 tool calling 스모크 결과

`scripts/runtime/smoke_tool_calling.py`로 텍스트 서버가 OpenAI 형식 `tool_calls`를 돌려주는지 확인한 기록입니다.
지원하지 않는 서버에서는 에이전트 루프가 매번 멈추고(`tool_calls`가 없으면 `no_tool_call`, `tools` 요청을 거부하면 `model_error`) 기존 파이프라인으로 폴백합니다. 결과는 같지만 모델 호출이 한두 번 늘어납니다.

| 날짜 | 서버 | 모델 | 결과 | 비고 |
|---|---|---|---|---|
| 2026-10-14 | MLX Serve (로컬) | ddalcu/Qwen3.8-27B-MLX-Serve-4bit | 미실행 | 구현 시점에 MLX Serve가 떠 있지 않았음 (`127.0.0.1:11234` 연결 거부). 모델 서버를 띄운 뒤 `PYTHONPATH=src python scripts/runtime/smoke_tool_calling.py --url http://127.0.0.1:11234 --model ddalcu/Qwen3.8-27B-MLX-Serve-4bit`로 확인 |
| — | SGLang (Stage) | qwen-text | 미실행 | Task 7의 `--tool-call-parser` 배포 후 확인 |

FAIL이면 로컬 `.env`에 `AGENT_MODE=pipeline`을 둡니다.
