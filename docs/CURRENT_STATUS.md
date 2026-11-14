# 현재 소스 — Room Hub 0.1.7

## PR #37의 Odyssey 단독 운영 목표

PC를 추론 경로에서 제거하는 실행 프로필 `HUB_AI_PROFILE=odyssey_openai`를 제공합니다. iPad가 Odyssey에 요청하고 Odyssey가 FFmpeg·규칙·예산·승인·SQLite를 관리하며 OpenAI STT/플래너를 직접 호출합니다. 기존 PC의 Whisper/Qwen·SSH 터널·포트 probe와 폴백은 이 프로필에서 차단됩니다. 세 관리자 전송 정책과 서버 API 키는 별도로 필요합니다.

이것은 소스의 지원 구성입니다. PR 병합·운영 서버 업데이트·실제 유료 API·iPad 검증이 완료됐다는 기록은 아닙니다. 적용 순서와 확인 범위는 [Odyssey 단독 운영](ODYSSEY_OPENAI.md)을 기준으로 사용합니다. 기존 설치가 Git인지 확인하지 않고 pull/강제 덮어쓰기를 하지 않습니다.

## 이전 V35 운영 기록 — 2026-09-20

당시 GitHub 기준은 모든 0.1.7/의존성 PR이 병합된 `main`이었습니다. 2026-09-20 사용자는 V35 운영 서버에 0.1.7 업데이트 패키지를 적용했다고 확인했습니다. 당시 `~/room-hub`는 Git 저장소가 아닌 패키지 설치 디렉터리였으므로, **그 기록만으로 현재 Odyssey의 Git 여부나 main과의 commit/tree 동일성을 확인할 수 없습니다.**

당시 V35는 Whisper multilingual Base / ko / 6T / careful beam5, Qwen CPU6T / ctx1024 / non-thinking, 8088/8443/8090/8022/8080 구조를 사용했습니다. 모델·DB·토큰·인증서·서비스 설정은 Git 소스와 분리하며 저장소에 포함하지 않습니다. 이 모델·포트 구성은 Odyssey OpenAI 프로필의 요구사항이 아닙니다.

메모와 알람은 관리자/위젯 서비스로 구현했습니다. 비공개 메모는 표시 기기에 보내지 않습니다. 알람은 실행 중인 서버가 예약을 판단하고, 허용한 활성 웹페이지가 소리를 재생합니다. 잠금/브라우저 종료 푸시·기기 깨우기·음성 비서의 메모/알람 실행은 미구현입니다. V35에 0.1.7이 설치되었다는 사실과 실제 iPad 알람 소리·절전·장시간 운용 검증은 구분합니다.

Git 관리형 전환 후에는 `deploy/termux/update-from-git.sh --check`의 local/remote HEAD·tree, ahead/behind, working tree 결과를 동일성 기준으로 사용합니다. 이후 개발은 **feature branch → PR → 사용자 검토/Merge → main CI 성공 → V35 안전 업데이트** 순서로 진행합니다.

[기능](NOTES_ALARMS.md) · [0.1.7 적용](UPDATE_017.md) · [검증](TEST_REPORT_017.md) · [V35 Git 업데이트](V35_GIT_UPDATE.md)
