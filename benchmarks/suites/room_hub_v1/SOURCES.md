# Research references used for Benchmark V1 design

이 파일의 외부 자료는 **범주/스키마/테스트 설계 참고용**입니다. 본 ZIP의 250개 한국어 발화는 모두 새로 작성했으며, 아래 데이터셋의 문장을 복제하지 않았습니다.

## Home Assistant / HassIL
- https://github.com/OHF-Voice/intents
- https://developers.home-assistant.io/docs/voice/intent-recognition/

참고점:
- sentence template + slot 구조
- intent별 테스트 문장 관리
- optional/alternative 표현과 slot 조합
- 저전력 장치에서 지나치게 큰 문장 permutation을 피하는 설계

## Rhasspy
- https://github.com/rhasspy/rhasspy/blob/master/docs/whitepaper.md
- https://github.com/rhasspy/rhasspy/blob/master/docs/intent-handling.md

참고점:
- transcript와 intent/slot JSON을 분리
- intent recognition과 intent handling을 별도 단계로 취급
- 원문(raw text)과 정규화된 text를 함께 보존하는 개념

## MASSIVE
- https://github.com/alexa/massive

참고점:
- 범용 음성 비서 발화를 domain / intent / slot으로 주석하는 방식
- 다양한 assistant scenario를 단일 NLU 평가셋으로 묶는 설계
- 52개 언어, 60 intent, 55 slot type의 대규모 NLU 평가 철학

## Korean intent/slot research
- https://www.kci.go.kr/kciportal/landing/article.kci?arti_id=ART002680101
- https://huggingface.co/datasets/wicho/kor_3i4k

참고점:
- 한국어에서도 intent classification과 slot filling을 별도로 평가할 필요
- 구어 한국어의 명령/질문/진술 차이 및 어순·표현 변형 고려

## NeMo joint intent/slot documentation
- https://github.com/NVIDIA/NeMo

참고점:
- task-oriented assistant에서 intent classification + slot extraction을 함께 평가하는 일반적 구조
