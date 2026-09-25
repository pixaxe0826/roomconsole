# M3.2 — Semantic Residue Precision

## 목적

M3.2는 M3.1에서 넓어진 Semantic Parser 진입 범위의 precision을 높이는 좁은 보정입니다.
신규 capability를 추가하지 않고, 자연어 목록 질문의 남은 문구를 단일 entity 제목으로 오인하거나
미지원 검색/복합 요청을 부분 실행하는 문제를 막습니다.

제품 VERSION은 0.1.7을 유지하고 Semantic Parser는 1.2.0으로 올립니다.
Benchmark engine은 1.3.0을 유지합니다. 실제 외부 250문항 데이터셋과 기존 결과는 수정하지 않습니다.

## 조회 residue

domain/date/status를 소스 근거로 처리한 뒤 남은 문구를 다음 순서로 다룹니다.

1. 검색·포함·관련 조건 → UNSUPPORTED_SEARCH_FILTER
2. 제한된 collection scaffold → LIST
3. 소비되지 않은 시간·범위 조건 → UNSUPPORTED
4. 그 외 연속 literal → 기존 GET + 서버 target resolver

non-empty residue 자체를 entity 이름의 증거로 사용하지 않습니다.

여러 domain이 같이, 한 번에, 랑, 하고 등으로 결합된 read는
MULTI_INTENT_UNSUPPORTED로 막습니다. 이 패치는 multi.read를 등록하지 않습니다.

## 시간대

기존 오전/오후 period를 재사용하며, 정확히 경계가 명시된
아침부터 낮 전까지와 낮부터 저녁 전까지만 각각 morning/afternoon으로 연결합니다.
그 외 before/after 시각 필터와 모호한 표현을 임의로 이 범위에 포함하지 않습니다.

## 동결 범위

- operation registry / DB schema / UI / timer ownership 변경 없음
- model, prompt, Whisper 변경 없음
- fuzzy target matching, 대명사/다중 턴 context guessing 없음
- scoring.py / reports.py / taxonomy.py / 외부 gold/projection 변경 없음
- Calendar add의 all-day와 missing-time 정책은 이번 패치에서 제목 의미로 추측하지 않음

## 검증

CI의 전체 pytest와 브라우저/HTTP/Docker 회귀를 유지합니다.
새 synthetic 테스트는 실제 private benchmark 문항을 복사하지 않고 같은 실패 구조만 재현합니다.
실제 M3.2 개선 여부는 병합 후 V35에서 새 run/analysis를 생성하고 M3.1 분석과 case-level compare로 판정합니다.
