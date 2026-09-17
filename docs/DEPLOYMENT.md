# 배포·상시 운영·데이터 보존

[문서 홈](../README.md) · [보안 경계](../SECURITY.md)

## 1. GitHub와 실행 서버는 다릅니다

GitHub 저장소는 소스와 변경 이력을 보관합니다. GitHub에 푸시해도 집의 Room Hub 서버가 자동으로 실행되지 않습니다.
GitHub Pages는 정적 사이트 호스팅이므로 이 프로젝트의 Python API·SQLite·WebSocket 서버를 대신하지 않습니다. 별도의 Pages 배포 워크플로도 넣지 않았습니다.

실제 서버는 Windows, Linux 또는 지원되는 NAS 컨테이너 환경에서 실행해야 합니다. 서버를 중지하면 데이터 갱신·원격 제어·완료 저장이 중단됩니다. 이미 열린 iPad 화면은 마지막 데이터를 유지할 수 있지만 오프라인 재시작은 지원하지 않습니다.

공식 참고: [GitHub Pages의 범위](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)

## 2. Windows 상시 운영

모니터 꺼짐과 PC 절전을 구분하세요. 상시 서버로 사용하는 동안 PC의 자동 절전·최대 절전은 사용하지 않고, 실제 서버 프로세스를 유지해야 합니다. PC를 종료하면 서버도 멈춥니다.

작업 스케줄러를 사용하는 경우 `START_WINDOWS.bat` 대신 가상환경 Python으로 `run.py`를 직접 실행하도록 설정합니다.

```text
프로그램: C:\room-hub\.venv\Scripts\python.exe
인수:     run.py
시작 위치: C:\room-hub
```

경로는 실제 설치 위치로 바꾸고, 사용자 로그온 여부와 관계없이 시작·실패 시 재시작·실행 시간 제한 해제를 검토합니다. 실행 계정에 코드와 데이터 경로 접근 권한이 필요합니다. `.env`를 자동으로 읽지 않으므로 서비스 계정의 실제 환경과 데이터 경로를 확인하세요.

## 3. Docker Compose

```bash
docker compose config --quiet
docker compose up -d --build
docker compose logs room-hub
```

- 이미지: 이 소스에서 직접 빌드하는 `room-hub:0.1.1`. 공개 레지스트리에 업로드되어 있다는 뜻이 아닙니다.
- 프로세스: **1 worker**. 여러 worker나 여러 복제본은 현재 인메모리 WebSocket 연결 관리와 맞지 않습니다.
- 데이터: `room-hub-data` named volume → 컨테이너 `/app/data`.
- 위젯: 현재 호스트의 `./widgets` → `/app/widgets`, 읽기 전용.
- 실행 계정: UID/GID `10001:10001`.
- 재시작 정책: `unless-stopped`.

업데이트는 **기존 Compose 프로젝트 폴더와 프로젝트 이름을 유지**하여 실행합니다.

```bash
docker compose up -d --build room-hub
```

폴더 이름이나 Compose 프로젝트 이름을 바꾸면 다른 named volume이 생성되어 데이터가 비어 보일 수 있습니다. 프로젝트 이름을 이미 사용 중이라면 같은 `-p` 옵션을 유지하세요. **`docker compose down -v`는 사용하지 마세요.**

직접 Python 실행의 `./data`는 기본 Compose 볼륨에 자동으로 들어가지 않습니다. 데이터 마이그레이션 없이 실행하면 새 서버로 시작합니다.

## 4. TrueNAS

`deploy/truenas.example.yaml`은 **Docker Compose 기반 사용자 정의 앱을 지원하는 환경용 예시**입니다. TrueNAS 호스트에 임의로 pip/Docker를 설치하는 절차가 아닙니다.

1. 소스와 데이터 Dataset을 분리합니다.
2. YAML의 `POOL`, 코드·위젯·데이터 절대 경로를 모두 바꿉니다. 디렉터리 이름이 `room_hub`인지 `room-hub`인지도 실제 값과 맞춥니다.
3. 컨테이너 UID/GID `10001:10001`이 데이터 Dataset에 쓸 수 있도록 권한을 설정합니다.
4. 로컬 `build` context를 허용하지 않는 앱 UI는 먼저 이미지를 빌드하고 접근 가능한 이미지로 지정해야 합니다.
5. 배포·로그·상태를 확인한 뒤 새 서버 주소로 관리자와 iPad를 연결합니다.

이번 소스 통합 작업에서 실제 TrueNAS 설치나 Docker 컨테이너 운영을 검증하지 않았습니다. [공식 사용자 정의 앱 문서](https://apps.truenas.com/managing-apps/installing-custom-apps/)

## 5. 백업

**코드의 Git 백업과 개인 데이터의 백업은 별개입니다.** 다음을 비공개 위치에 보관합니다.

- `data/` 전체: DB, WAL/SHM 파일이 남아 있다면 그 파일들, 관리자/입력 키, 음원.
- 사용자 위젯 소스와 실제 환경 설정.
- Docker 사용 시 named volume 또는 바인드 마운트 데이터 경로.

서버를 정상 종료한 뒤 데이터 폴더 전체를 복사하는 것이 간단합니다. 실행 중 DB만 파일 복사하지 말고, 관리자의 DB 백업 기능을 사용하세요. 서버 백업 API는 SQLite backup API로 일관된 DB 사본을 생성합니다. 음원 파일과 키·환경 변수는 별도 보존이 필요합니다.

JSON 내보내기는 데이터 열람용이며 이 버전에 JSON 복원 UI는 없습니다.

## 6. DB 복원

```bash
# 반드시 실제 서버를 먼저 중지하세요.
python scripts/restore_db.py /private-backup/room-hub.sqlite3 \
  --data-dir /private-data/room-hub --server-stopped
```

복원 도구는 기존 DB를 백업하고 무결성을 검사합니다. 복원된 세션·연결 코드는 제거하고 표시 기기를 해제하므로 로그인·페어링을 다시 해야 합니다. `--server-stopped`는 확인 플래그일 뿐 프로세스를 자동 감지하거나 중지하지 않습니다.

운영 키·DB·음원·백업 ZIP을 GitHub 이슈나 Releases에 올리지 마세요.
