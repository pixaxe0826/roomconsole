# NLU reference code review — M3.3-B.1

## 확인한 실제 소스와 적용 범위

아래는 이번 작업에서 직접 읽은 upstream 코드다. 버전이 움직이는 `main` 링크 대신
검토한 commit SHA를 고정한다. Room Console의 구현은 설계 패턴을 적용한 자체 코드이며
upstream 파일을 복사하거나 라이브러리를 vendor/install하지 않았다. 새 의존성은 없다.

| 프로젝트 / 고정 revision | 읽은 코드 | 가져온 설계 / 실제 적용 |
|---|---|---|
| HassIL `e236373b66fcba15ba93fffb1f86ca911e3136da` | `hassil/recognize.py` (context/slot-list/expansion matching와 raw offset 추적), `hassil/sample.py` (`sample_intents`, `sample_sentence`) | 문장 유형을 선언 데이터로 분리하고 원문 capture를 보존. `memo_language.SentenceFamily`, `recognize`, `LanguageMatch` 및 합성 문장 검사 도구에 적용 |
| OVOS Core `c833369cf7aba59c5a6f1e93d1799e75a8133cb1` | `ovos_core/intent_services/service.py` (typed-slot 단계, `handle_utterance` pipeline 반복, required-slot 검사, dispatch 분리) | matcher의 적용 순서와 실제 실행을 분리. safety → selector/content-first → explicit naming → literal name. 같은 단계의 상충하는 의미는 거부하도록 Room Console에서 더 보수적으로 구현 |
| Rasa SDK `b20db6130e57db11c4249970dc0c7c2a6024cade` | `rasa_sdk/forms.py` (`get_extraction_events`, `get_validation_events`, `next_requested_slot`) | 추출과 검증을 분리하고 현재 요청 중인 슬롯을 유지하는 패턴. 기존 M3.3-A typed dialogue는 교체하지 않고, 제목 답변 칸에 들어온 메모 명령이 별도 실행되지 않는 통합 테스트를 추가 |
| Rhasspy NLU `06d118c0ff92cec95e0e70a9c08b12b7f5202f46` | `rhasspynlu/intent.py` (`Entity`, `Recognition`, raw/normalized value 및 offset 구분), 저장소의 template 설명 | 값과 원문을 구분. wrapper 제거 후 target만 정규화하고 본문·각 capture의 raw span은 그대로 보존. FST/ASR decoder는 가져오지 않음 |

### 고정 소스 링크

- [HassIL recognition](https://github.com/OHF-Voice/hassil/blob/e236373b66fcba15ba93fffb1f86ca911e3136da/hassil/recognize.py)
- [HassIL sampling](https://github.com/OHF-Voice/hassil/blob/e236373b66fcba15ba93fffb1f86ca911e3136da/hassil/sample.py)
- [OVOS intent service](https://github.com/OpenVoiceOS/ovos-core/blob/c833369cf7aba59c5a6f1e93d1799e75a8133cb1/ovos_core/intent_services/service.py)
- [Rasa form validation](https://github.com/RasaHQ/rasa-sdk/blob/b20db6130e57db11c4249970dc0c7c2a6024cade/rasa_sdk/forms.py)
- [Rhasspy entity representation](https://github.com/rhasspy/rhasspy-nlu/blob/06d118c0ff92cec95e0e70a9c08b12b7f5202f46/rhasspynlu/intent.py)

검토한 파일의 Git blob SHA:

```text
hassil/recognize.py                   4c6998cfaf3b5b79dcfae78336440457090b4c43
hassil/sample.py                      1f72b92a2cd5809225b88090038433fcc7f4692e
ovos_core/intent_services/service.py  8e67a717bf085d816579879e860ffdf74bb84b00
rasa_sdk/forms.py                     1288923aa2bbd8430b2d7936787f2bc20fdaa65d
rhasspynlu/intent.py                   5d3f537e2048a0e0665bdee5318e37fea3a59a47
```

## 이번에 전체 프레임워크를 도입하지 않은 이유

이는 upstream 제품의 성능 열위 주장이나 V35에서 실행 불가능하다는 판정이 아니다.
현재의 목적은 이미 운영하는 서버에서 메모 회귀를 좁게 복구하는 것이다. 패키지 전체를
도입하면 배포·버전·한국어 slot 처리와 기존 권한 경계를 동시에 재검증해야 한다.
이번에는 공통 패턴만 적용하고 새로운 dependency, 별도 서버, 모델, 서비스는 추가하지 않는다.

`memo_language.py`는 **Python 정규식 기반의 제한된 선언형 테이블**이다. 완전한 HassIL
문법 파서/YAML DSL도, Rasa Forms 재구현도, OVOS pipeline plugin도 아니다. 이 구분을
보고서와 benchmark에 유지한다. Memo 외 전 영역이 이 테이블로 이전된 것은 아니다.

## 정규식 추가와 무엇이 다른가

문장마다 action뿐 아니라 `stage`, `disposition`, `selector`, `issue`가 함께 정의된다.
`recognize`는 전체 문장에 대한 후보들을 모으고, 가장 앞선 단계의 의미가 유일할 때만
선택한다. 선언 순서가 바뀌어도 의미가 바뀌지 않도록 검사하며 같은 단계에서 서로 다른
동작을 제안하면 명확화를 요구한다. 더 낮은 단계에서 매칭했던 문법 이름도 기록한다.

단어가 문장 어디엔가 존재한다는 이유만으로 selector 권한을 주지 않는다. 특히 본문이
앞에 나오는 append 문장은 전체 `본문 + 라고 + 현재/지금 메모 + 덧붙이기` 구조가
확인될 때만 기존 처리로 delegate한다. 대명사 참조·검색 제약은 named title보다 먼저
명시적으로 거부하고, 후순위 LLM이 그 조건을 지워 기본 메모를 읽게 하지 않는다.

문법 데이터만으로 실제 실행을 허가하지 않는다. 기존 WidgetRegistry, 원문 검증,
Entity Catalog, preview, manager confirmation, version 및 receipt가 실행 권한을 결정한다.

## 합성 확장 검사의 한계

`scripts/check_memo_language.py`는 합성 제목·문구와 문법 조합을 생성해 family, capture,
전체 문장 경계를 검사한다. 생성기가 문법과 표현 일부를 공유하므로 이를 독립적인
실제 사용자 정확도나 ASR benchmark로 주장하지 않는다. 별도 수작업/부정/HTTP/대화
경계 테스트와 사용자의 외부 250문항을 함께 사용해야 한다. 생성물을 Git에 dataset으로
추가하거나 사용자 평가 gold를 문법에 복사하지 않는다.

[구현 및 V35 인수 안내](MEMO_LANGUAGE_M33B1.md)
