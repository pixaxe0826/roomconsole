# 실내 온도·실내 공기 질 통합 패치 v3 — 검증 결과

검증일: **2026-10-26, 한국시간 기준**. 사용자가 제공한 `/api/sensors` 응답을 합성 입력으로 만들어 검증했습니다. 실제 사용자 LAN·NodeMCU·Odyssey에 접속하거나 운영 서비스를 변경하지 않았습니다.

## 1. 최종 결과

| 검사 | 결과 |
|---|---:|
| 전체 Python 회귀 | **1,791 passed, 14 skipped, 0 failed** |
| 위 전체 검사에 포함된 기존 온도 계약·소스 정책 | 37 passed |
| 위 전체 검사에 포함된 기존 HTTP 수집·마이그레이션·lifecycle | 46 passed |
| 위 전체 검사에 포함된 신규 통합 센서·두 표시 API | **63 passed** |
| 위 전체 검사에 포함된 설치·업그레이드·롤백 | **44 passed** |
| 실제 완성 runtime ZIP 설치·재적용·롤백 | **6개 경로 모두 통과** |
| 두 신규/수정 위젯 Chromium 검사 | **93/93** |
| 기존 HTTP·기본 화면·할 일·목록·65초 미리보기 | **243/243** |
| 기존 LLM UI·Assistant·대화·메모·생활·타이머 UI | **247/247** |
| 저장소 정책·문서 링크 | 356개 파일 / 145개 링크, 오류 0 |
| 외부 벤치마크 데이터 정책·타이머 통신 Node 검사 | 통과 |
| 메모 문법 생성 템플릿 | positive 320 / negative 320, 통과 |
| 기존 합성 fast-read 벤치마크 | 각 입력 30회, 정상 완료 |

동일 검사를 중복 합산하지 않았습니다. 이 숫자는 소프트웨어 합성 테스트의 통과 수이며 센서 정확도나 실기기 연결 성공률을 뜻하지 않습니다.

## 2. API 계약과 두 위젯

기본 수집 주소는 사용자가 제공한 **`http://192.168.0.22/api/sensors`**입니다. 설치 후 CLI로 원본 하나를 등록하며, 설정 전에는 외부 센서 요청을 만들지 않습니다.

| 원본 필드 | 표시 API |
|---|---|
| temperature_c / humidity_percent / pressure_hpa | 실내 온도 |
| dust_adc_raw / dust_a0_voltage_v / dust_sensor_voltage_v | 실내 공기 질 |

- 모든 수치는 제공된 값을 그대로 사용합니다. 소수 ADC를 정수로 자르거나 전압을 다른 측정값으로 환산하지 않습니다.
- 여섯 필드는 유한한 JSON 숫자여야 합니다. 문자열·boolean·null·NaN·Infinity와 유효한 0을 구분합니다. 추가 필드는 무시합니다.
- 필수 값 하나라도 잘못되면 해당 응답 전체를 저장하지 않습니다. 같은 성공 응답의 여섯 값을 한 트랜잭션으로 저장하므로 서로 다른 응답의 값·성공 시각을 섞지 않습니다.
- 온도 -40~125, 습도 0~100, 기압 0 초과~2000, ADC 0~65535, 전압 0~100의 넓은 입력 검사를 적용합니다. 앱의 입력 검증 범위이며 하드웨어 정격·ADC 해상도·공기 질 등급 기준이 아닙니다.
- 먼지 값에는 농도 보정식이 없으므로 **PM2.5·PM10·AQI·µg/m³·좋음/나쁨 등급을 만들지 않습니다.** 화면에 `농도 미보정`을 표시합니다.
- 성공 시각은 Odyssey가 유효한 응답을 받은 시각입니다. 원본에 측정 시각·센서별 상태가 없으므로 실제 새 측정 수행 여부를 확인하지 않습니다.

두 위젯은 같은 `room-temp-01` 수집 원본을 공유합니다. 한 background worker가 10초 주기로 원본을 조회하며, 두 표시 API를 여러 번 읽어도 NodeMCU 요청을 추가하지 않는 것을 검증했습니다.

기존 `/api/temperature`와 push 계약은 호환용으로 유지했습니다. 기존 온도 ADC·전압을 먼지 측정으로 표시하지 않으며, 공기 질 위젯은 새 통합 원본 설정이 필요하다고 안내합니다. 이번 최초 설치에는 이전 패치·펌웨어 업로드·보드의 Room Hub 토큰이 필요하지 않습니다.

## 3. 백엔드·저장소·권한

Python 3.12.14 / Linux / FastAPI 0.141.1 / Starlette 1.10.0 / httpx 0.28.1 / pytest 9.1.1 환경에서 전체 회귀를 실행했습니다.

```bash
HUB_DATA_DIR=artifacts/sensors-v3-runtime python -m pytest -q \
  --timeout=30 --timeout-method=thread \
  --junitxml=artifacts/test-results/sensors-v3-backend.xml
```

전체 검사 시간은 **170.42초**였습니다. 14개 skip은 기존 nginx/OpenSSL 실행 도구 조건 검사 11개와 Windows cmd.exe/PowerShell 조건 검사 3개입니다. Starlette의 httpx TestClient deprecation warning 한 건이 있었습니다.

주요 검증 범위:

- HTTP·명시적 RFC1918 IPv4·정확한 `/api/sensors` 또는 호환 `/api/temperature` 경로만 허용. DNS 호스트·사용자 정보·query·fragment·공인/loopback/link-local 주소·잘못된 포트·다른 경로 거부.
- 환경 프록시 비사용, redirect 미추적, Room Hub 인증·표시 쿠키를 센서에 보내지 않음.
- 3초 I/O timeout, 전체 fetch 4초 상한, 응답 2KiB 제한·압축 응답 거부.
- HTTP 오류·timeout·연결 실패·잘못된 JSON·필수 값 오류가 마지막 정상 값·성공 시각을 갱신하지 않음. 최초 실패를 가짜 0으로 만들지 않음.
- 마지막 성공 후 30초 지연·120초 연결 끊김과 정상 응답 후 복구.
- 같은 ID·URL 설정은 멱등적. URL 변경·원본 재활성화는 새 revision을 만들고 현재 모든 측정값을 비움.
- 설정 변경·해제 뒤 도착한 이전 응답은 revision과 enabled 재검사로 거부.
- 이전 이력 보존, 현재 revision만 그래프·최저/최고에 사용. 1분 대표값과 별도로 원시 표본 전체의 최저/최고 계산.
- v1/v2 SQLite에 새 컬럼을 추가하면서 기존 값·기록 보존. 재시작 후 새 여섯 값 유지.
- 앱 lifespan 시작 시 수집, 종료 시 worker/client 정리, 기존 lifespan state 보존.
- 표시 API마다 해당 종류·device_id 위젯의 공유 배치 존재를 검사. 온도 위젯만으로 공기 질 API를 열지 않음. 관리자 인증은 기존 방식 유지.
- 표시 API에 해당 위젯 데이터만 전달하고 URL·토큰·revision 미노출. 표시 기기 해제 후 접근 취소.

센서 저장소는 기존 Room Hub 데이터와 별도인 `indoor-temperature.sqlite3`입니다. 연결은 명시적으로 닫습니다. 원시 이력의 24시간 정리는 다음 정상 수집 때 수행하므로 수집이 중지되면 오래된 행이 남을 수 있습니다.

## 4. 두 화면과 기존 기능

Playwright 1.56.0 / Chromium 141.0.7390.37에서 실제 production JS·CSS를 실행했습니다. 두 위젯 검사 **93/93 통과**, uncaught JavaScript error와 외부 네트워크 요청은 각각 0건입니다.

- `1194×834`, `834×1194`, `1024×768`의 기본 2×2 및 가로·세로 확대 화면.
- 온도·습도·기압 매핑과 먼지 ADC 소수·전압 소수점 네 자리 표시.
- 최초 대기·즉시 실패·30초 지연·120초 끊김·회복, null/0/숫자 문자열 구분.
- strict CSP·동일 출처 GET, 브라우저의 NodeMCU 직접 요청 없음.
- 같은 위젯의 기본/확대 조회 공유, 미완료 요청 중복 방지, unmount abort, 늦은 응답 무시, polling 정리.
- 401·403·404에서 이전 값 제거와 사용자 설정 문자열 escape.
- 두 위젯 동시 배치 시 상태·CSS 충돌 없음.
- 합성 미리보기의 예시 표시·실제 API 미호출·키보드 확대 탭 탐색.

독립 미리보기, 실제 세로 카드와 확대 화면을 시각 검수했습니다. QA용 한글 폰트는 제품에 포함하지 않았고 외부 폰트 요청도 추가하지 않았습니다.

| 기존 회귀 | 통과 |
|---|---:|
| live_smoke / browser_smoke | 19 / 27 |
| todo_regression / todo_transport | 36 / 15 |
| all_tasks_regression / all_tasks_transport | 48 / 11 |
| preview_regression, 65초 관측 | 87 |
| llm_regression / llm_widget_regression / llm_widget_live | 36 / 42 / 24 |
| assistant / dialog / memo_catalog | 19 / 12 / 12 |
| context / life / fast_reads | 20 / 22 / 15 |
| widget_bridge / timers | 20 / 25 |

기존 UI 회귀 스크립트는 수정하지 않았습니다. `memo_catalog_browser`의 assertion과 `page_errors` 검사는 통과했지만 종료 시 Python 브리지의 미완료 future에 기존과 같은 `TargetClosedError` 경고 두 건이 남았습니다. 별도로 기록했으며 새 위젯 오류로 계산하지 않았습니다.

## 5. 통합 최초 설치·롤백

설치 패치 ID는 **`room-hub-indoor-sensors-v3`**입니다. 실내 온도 위젯은 3.0.0, 새 실내 공기 질 위젯은 1.0.0입니다.

**이전 패치가 없는 설치본에 직접 적용할 수 있습니다.** 최초 설치는 기능 파일 8개와 기존 main.py에 넣는 연결 두 줄로 구성됩니다.

- 읽기 전용 `--check`와 기본 동작은 파일을 바꾸지 않습니다.
- main.py의 실제 AST·인증·인접 통합 지점을 검사하고 다른 원본 바이트는 보존합니다.
- SHA256·위젯 디렉터리별 manifest ID 검사. 알 수 없는 기존 파일 편집은 덮어쓰지 않습니다.
- 서비스 중지와 실제 8088 연결 거부 확인, 백업·원자적 교체·실패 복구·소유권·모드 유지.
- 같은 payload 재적용은 무변경. 설치 뒤의 로컬 수정은 롤백 전에 충돌로 검출합니다.
- 정확히 알려진 v1/v2 파일의 선택적 업그레이드도 지원하지만 이번 사용자 설치에는 필요하지 않습니다.
- `app/store.py`는 설치기가 읽거나 교체하지 않으므로 기존 ClosingConnection 수정이 유지됩니다. 데이터·설정·레이아웃·모델·기존 서비스 설정은 설치 대상이 아닙니다.

기준은 `pixaxe0826/roomconsole`의 `d8e2f0554de4fb44c33da59c36d129098fbdeabd`입니다. PR #29 head `09df1f7dad5a4f9cbb742647c75023c542a1d0e9`의 실제 main.py도 검증했고 Git blob SHA가 일치함을 확인했습니다. 실제 Odyssey 설치본 전체를 읽은 것은 아니므로 운영 적용 전 `--check`가 필요합니다.

최종 runtime ZIP에서 추출한 실제 설치기와 8개 payload로 **main/PR29 × 최초 설치/v1 업그레이드/v2 업그레이드의 6개 경로**를 검사했습니다. 전 경로에서 읽기 전용 검사, 바이트 일치, 재적용, 원본 바이트·모드·소유권·생성 디렉터리 롤백, 합성 store FD 수정·데이터 마커 보존을 확인했습니다.

runtime ZIP SHA256:

```text
e5128887b3c4d2259fa64a615be5d151d2cd82ebc4e153d3b50acfaeee35443f
```

최종 사용자 ZIP의 `update/`는 이 검증된 runtime ZIP의 설치기·payload를 그대로 담습니다. 안내·미리보기·검토용 source diff는 별도로 포함되며 펌웨어는 포함하지 않습니다. 최종 ZIP의 각 항목은 `MANIFEST_SHA256.json`으로 확인합니다. 해시는 손상 검사이며 전자서명은 아닙니다.

## 6. 실제 장비에서 확인할 범위

사용자의 `192.168.0.22`를 실제로 조회하지 않았습니다. Odyssey의 API 접근, NodeMCU의 현재 응답, 센서 정확도, iPad Safari, 장시간 운용, 보드 전원·Wi-Fi 끊김/복구와 재부팅은 해당 장비에서 확인해야 합니다. 실제 LLM 추론·Windows/Docker CI도 이번 검사로 주장하지 않습니다.

운영 배포·보드 펌웨어 업로드·GitHub push/PR 변경은 수행하지 않았습니다. 사용자는 패키지의 `ODYSSEY_GUIDE.md`에 따라 이번 ZIP 하나를 적용하면 됩니다.
