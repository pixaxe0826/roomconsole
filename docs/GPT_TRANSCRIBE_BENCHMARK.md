# GPT-Transcribe 같은 음성 비교

이 문서는 실행 방법입니다. 실제 인식 정확도·속도를 검증했다는 결과가 아닙니다.

## 기존 벤치마크와 구분

기존 `python -m benchmarks`의 외부 250문항 suite는 **텍스트 명령 해석·실행 정책** 평가입니다.
음성 녹음이 없으므로 이 suite로 전사 CER/WER를 측정할 수 없습니다.
현재 해당 엔진의 모델 연결 선택은 `disabled` 또는 레거시 `local`입니다.
OpenAI Personal Agent를 평가한 것으로 해석하지 마세요.
기존 데이터·gold·점수·이전 결과는 이 업데이트에서 변경하지 않습니다.

V35의 `deploy/termux/stt_accuracy_cli.py`는 별도의 Whisper 프로필 비교 도구입니다.
아래 OpenAI 측정은 그 도구나 운영 DB, speech 설정, 할 일·일정·메모를 사용하지 않습니다.

## 입력 준비

별도 **Git 밖**의 폴더에 테스트용 녹음과 사람이 확인한 정답을 준비합니다.
실제 사용자의 짧은 한국어 발화를 사용해야 운영 전사에 관한 근거가 됩니다.
TTS 합성 음성은 전송·계측 확인용이며, 실제 iPad 마이크 정확도를 대변하지 않습니다.

외부 JSONL 파일은 한 줄에 한 JSON 객체를 둡니다. 필드는 다음 네 개뿐입니다.

| 필드 | 내용 |
|---|---|
| `id` | 중복 없는 영문·숫자·점·밑줄·하이픈 식별자 |
| `audio` | manifest 폴더 아래 WAV 상대 경로. 상위 이동·링크 금지 |
| `reference` | 사람이 직접 확인한 정답 전사. 힌트나 모델 입력으로 전송하지 않음 |
| `language` | 선택 사항. 기본 `ko`, 두 글자 언어 코드 또는 `auto` |

manifest·정답·음원은 소스에 넣지 않습니다. WAV는 **16 kHz, mono, PCM 16-bit**로 미리 변환합니다.
원음은 덮어쓰지 않습니다. 변환이 필요하면 별도 출력 파일에 FFmpeg를 적용하세요.
오디오별 최대 10 MiB/300초, manifest 최대 2 MiB/500문항,
한 번에 선택한 오디오 총합 최대 256 MiB입니다. 기본은 앞 50문항입니다.
정답은 정규화 뒤 4,000자 이내이며 비어 있으면 측정하지 않습니다.

## 실행

API 키는 해당 테스트 셸에 `OPENAI_API_KEY` 또는 `HUB_OPENAI_API_KEY`로 안전하게 설정합니다.
키 값을 CLI 인수·명령 이력·채팅·소스에 넣지 마세요. 서비스 키가 설정되어 있어도
다른 테스트 환경에 자동으로 제공되는 것은 아닙니다. 운영 서비스 환경 전체를 가져오지 마세요.

먼저 호출하지 않는 사전 점검:

```bash
python scripts/benchmark_openai_stt.py
```

`NOT_RUN`과 `api_requests: 0`이 나옵니다. 키·외부 manifest·명시적인 `--live`가 없으면
정답·음원을 읽기 전에 멈춥니다. 키의 **계정 모델 접근 권한**은 이 존재 확인만으로 알 수 없습니다.

테스트용 음성 전송과 과금에 동의한 별도 실행 예시:

```bash
python scripts/benchmark_openai_stt.py \
  --manifest /external/room-hub-stt/cases.jsonl \
  --results-root /external/room-hub-stt-results \
  --name transcribe-comparison-01 \
  --live --max-cost-usd 5 \
  --models gpt-4o-mini-transcribe gpt-transcribe \
  --hints both --limit 50 --repeats 1 --audio-origin human
```

경로는 실제 외부 폴더로 바꿉니다. 결과 폴더는 데이터 폴더와 분리합니다.
기본 결과 위치는 Git에서 제외된 `artifacts/benchmarks/stt/`입니다.
같은 이름의 결과 폴더는 덮어쓰지 않습니다. 재시작·오류 뒤 자동 재개나 재시도하지 않습니다.
새 이름으로 다시 실행하는 것은 **새 유료 실행**입니다.

`--max-cost-usd`는 `--live`에서 필수이며 별도 실행의 USD 1~100 한도입니다.
운영 월 예산은 변경하지 않습니다. 각 호출 전에 앱 예산 거버너가 예약하며,
자동 모델 다운그레이드·로컬 fallback은 꺼져 있습니다. 누락 usage는 0달러로 정산하지 않습니다.
실패 또는 미정 비용을 만나면 나머지 요청을 멈추고 예약을 결과 폴더의 독립 DB에 남깁니다.
이 상한은 앱이 알고 있는 단가·공급자 usage에 근거하며 공급자 계정의 청구 한도 설정은 아닙니다.

기본 비교는 다음 네 조건입니다. 문항·반복마다 순서를 순환하여 고정 순서 편향을 줄입니다.

- `gpt-4o-mini-transcribe`, 힌트 없음
- `gpt-4o-mini-transcribe`, Room Hub 고정 어휘 힌트
- `gpt-transcribe`, 힌트 없음
- `gpt-transcribe`, Room Hub 고정 어휘 힌트

작업 제목·메모·실제 DB를 읽지 않습니다. 정답에서 어휘를 추출하지 않습니다.
`--hints roomhub`는 두 모델의 힌트 적용 조건만 비교하고, `--repeats 2`는 같은 입력을 두 번 과금합니다.
측정은 STT에만 한정되며 결과 전사문을 에이전트에 제출하거나 작업으로 실행하지 않습니다.

## 결과 읽기

`report.json`에는 입력·정답·전사문의 해시, 문항별 점수, 요청 모델과 실제 선택 모델,
지연, 비용 확인 여부, 합산 지표를 저장합니다. 음원·정답 원문·전사 원문·키는 복사하지 않습니다.
`budget.sqlite3`는 이 실행만의 예약·사용량 원장입니다. 결과 폴더는 공유 자료로 올리기 전에 검토하세요.

- **CER**: 기존 Whisper 비교와 같은 NFC/casefold, 공백·구두점 제외 문자 편집 거리입니다.
  문항별 비율의 단순 평균이 아니라 전체 편집 수/전체 정답 문자 수로 합산합니다. 1을 넘을 수 있습니다.
- **WER**: 공백 단위 단어 편집 거리입니다. 한국어 형태소 평가가 아니므로 띄어쓰기 차이에 민감합니다.
- **실패/미평가**: 성공한 전사만 CER/WER를 계산하고 나머지는 실패·미평가 수로 따로 남깁니다.
  조건별 측정 수가 다르면 숫자만 비교하여 승자를 정하지 마세요.
- **p50/p95**: gateway의 API 왕복 시간입니다. 녹음·업로드·FFmpeg·에이전트 실행 시간은 제외합니다.
  표본이 적거나 네트워크·공급자 부하가 다르면 p95를 운영 SLA처럼 해석할 수 없습니다.
- **비용**: 공급자 usage가 확인된 호출만 정산합니다. 미정 비용이나 미실행 조건을 0달러로 표시하지 않습니다.
  앱 계산은 실제 청구서와 대조가 필요합니다.

기존 250문항을 실행하려면 [기존 외부 데이터 벤치마크](ASSISTANT_BENCHMARK.md)를 따릅니다.
그 결과와 이 전사 결과는 다른 지표이며 하나의 정확도로 합치지 않습니다.

## 합성 회귀

```bash
python -m pytest -q tests/test_openai_stt_benchmark.py
python scripts/check_benchmark_policy.py
```

CI는 임시 폴더에서 생성한 작은 manifest·WAV와 모의 HTTP 응답만 사용합니다.
힌트에 정답 누출 금지, 같은 음성 비교, 예산·미정 비용 유지, 중단·덮어쓰기 방지,
기존 CER와의 합성 입력 동일성 등을 확인하며 실제 모델 인식률을 측정하지 않습니다.
