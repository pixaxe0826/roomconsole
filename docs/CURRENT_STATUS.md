# 현재 소스 — Room Hub 0.1.7

GitHub 기준은 모든 0.1.7/의존성 PR이 병합된 `main`입니다. 2026-09-20 사용자는 V35 운영 서버에 0.1.7 업데이트 패키지를 적용했다고 확인했습니다. 다만 현재 `~/room-hub`는 아직 Git 저장소가 아닌 패키지 설치 디렉터리이므로, **GitHub main과의 commit/tree 단위 동일성은 Git 관리 전환 전까지 검증할 수 없습니다.**

운영 V35는 Whisper multilingual Base / ko / 6T / careful beam5, Qwen CPU6T / ctx1024 / non-thinking, 기존 8088/8443/8090/8022/8080 구조를 유지합니다. 모델·DB·토큰·인증서·서비스 설정은 Git 소스와 분리하며 저장소에 포함하지 않습니다.

메모와 알람은 관리자/위젯 서비스로 구현했습니다. 비공개 메모는 표시 기기에 보내지 않습니다. 알람은 실행 중인 서버가 예약을 판단하고, 허용한 활성 웹페이지가 소리를 재생합니다. 잠금/브라우저 종료 푸시·기기 깨우기·음성 비서의 메모/알람 실행은 미구현입니다. V35에 0.1.7이 설치되었다는 사실과 실제 iPad 알람 소리·절전·장시간 운용 검증은 구분합니다.

Git 관리형 전환 후에는 `deploy/termux/update-from-git.sh --check`의 local/remote HEAD·tree, ahead/behind, working tree 결과를 동일성 기준으로 사용합니다. 이후 개발은 **feature branch → PR → 사용자 검토/Merge → main CI 성공 → V35 안전 업데이트** 순서로 진행합니다.

[기능](NOTES_ALARMS.md) · [0.1.7 적용](UPDATE_017.md) · [검증](TEST_REPORT_017.md) · [V35 Git 업데이트](V35_GIT_UPDATE.md)
