# 배포·상시 운영·데이터 보존

[문서 홈](../README.md) · [V35/Termux](V35_TERMUX.md) · [보안](../SECURITY.md)

## GitHub와 실행 서버는 다릅니다

GitHub 저장소는 소스와 이력을 보관합니다. push해도 집의 Room Hub 서버가 자동 실행되지 않습니다. GitHub Pages는 Python API·SQLite·WebSocket을 대신할 수 없습니다.

## V35 상시 운영 — 현재 실사용 구성

현재 프로젝트의 주 서버는 LG V35/Termux입니다. Room Hub는 `termux-services`의 `room-hub`, HTTPS는 `room-hub-https` 서비스로 관리할 수 있습니다. 자세한 설치·상태 확인·Tasker 부팅 자동화는 [V35/Termux](V35_TERMUX.md)에 있습니다.

Windows PC는 서버가 아니라 필요할 때 `http://<V35-LAN-IP>:8088/manager`를 여는 관리자 단말로 사용할 수 있습니다.

## Windows 상시 운영

Windows 자체를 서버로 사용할 경우 PC 절전/최대절전을 별도로 관리해야 합니다. 작업 스케줄러에서는 `START_WINDOWS.bat` 대신 해당 프로젝트의 가상환경 Python으로 `run.py`를 실행하는 편이 명확합니다.

## Docker Compose

```bash
docker compose up -d --build
docker compose logs room-hub
```

- 이미지 태그: `room-hub:0.1.4`
- 서버 worker: 1
- 데이터: `room-hub-data` named volume
- 위젯: `./widgets` 읽기 전용 마운트
- 재시작 정책: `unless-stopped`

업데이트 시 기존 Compose 프로젝트/볼륨을 유지하세요. **`docker compose down -v`는 데이터 삭제 위험이 있으므로 사용하지 않습니다.**

## TrueNAS

`deploy/truenas.example.yaml`은 Docker Compose 기반 사용자 정의 앱을 지원하는 환경용 예시입니다. TrueNAS 버전/앱 런타임/ACL을 확인한 뒤 코드와 데이터 Dataset을 분리하세요. 이 저장소를 만들면서 실제 TrueNAS 배포를 재검증한 것은 아닙니다.

## 백업

코드의 Git 백업과 개인 데이터 백업은 별개입니다. 비공개 위치에 다음을 보관하세요.

- `data/` 전체: SQLite, token, 음원, speech/LLM 런타임 기록
- HTTPS CA/server private key
- 사용자 위젯과 실제 환경 설정
- Docker 사용 시 named volume 또는 바인드 마운트 데이터

서버 실행 중 DB 파일을 단순 복사하는 대신 SQLite backup 도구를 사용하거나 서버를 정상 정지한 뒤 전체 데이터 폴더를 백업합니다. 운영 키·녹음·DB·백업 ZIP을 GitHub에 올리지 마세요.
