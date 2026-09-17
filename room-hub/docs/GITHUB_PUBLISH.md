# GitHub 저장소 만들기 — Windows 기준

[문서 홈](../README.md)

## 1. 업로드할 폴더

이 ZIP을 풀면 `room-hub/`가 나옵니다. **그 안의 README.md, app/, web/, widgets/, .github/ 등이 저장소의 최상위**에 있어야 합니다.

```text
권장:   저장소/README.md, 저장소/app/...
피하기: 저장소/room-hub/README.md  (불필요한 중첩)
피하기: 저장소/room_hub_github_v0.1.1.zip 만 업로드
```

ZIP에는 실제 사용자의 데이터가 없습니다. 운영 중인 프로젝트를 통째로 복사해서 올리지 말고 이 깨끗한 소스 폴더부터 사용하세요. 숨김 파일 `.gitignore`, `.gitattributes`, `.github`도 포함해야 합니다.

## 2. 빈 원격 저장소 생성

GitHub에서 이름을 `room-hub` 등으로 정해 저장소를 만듭니다. 이 소스에는 README·gitignore가 있으므로 원격 저장소 생성 화면에서 별도 README·.gitignore·라이선스를 자동 생성하지 않습니다. 이미 파일이 있는 원격 저장소라면 먼저 clone한 폴더에 소스를 복사하고 충돌을 검토하세요. 강제 푸시로 기존 이력을 지우지 마세요.

프로젝트의 공개 범위와 라이선스가 정해지지 않았다면 Private으로 시작할 수 있습니다. 이 패키지는 원격 저장소를 생성하거나 공개 설정을 변경하지 않습니다.

## 3. PowerShell에서 최초 커밋

Git for Windows가 설치된 환경에서, 실제 압축 해제 위치로 이동합니다.

```powershell
cd C:\Projects\room-hub

git init -b main
python scripts/check_repo.py

git add .
git status --short
python scripts/check_repo.py --staged

git diff --cached --stat
git commit -m "Initial Room Hub 0.1.1 source"
```

`check_repo.py`는 Python 표준 라이브러리로 동작하므로 앱 의존성을 설치할 필요가 없습니다. 최초 Git 사용 시 이름·이메일 설정과 GitHub 인증을 요청할 수 있습니다. 커밋 이메일은 본인이 선택하고 공개 범위를 확인하세요.

다음 명령의 `YOUR_GITHUB_ID`와 저장소 이름을 실제 값으로 바꿉니다.

```powershell
git remote add origin https://github.com/YOUR_GITHUB_ID/room-hub.git
git push -u origin main
```

GitHub 토큰을 URL에 넣거나 코드에 적지 마세요. Git for Windows의 자격 증명 관리 또는 본인이 설정한 SSH 인증 경로를 사용합니다.

## 4. GitHub Desktop 방식

명령줄 대신 GitHub Desktop을 사용해도 됩니다. 로컬 `room-hub` 폴더에서 저장소를 초기화한 뒤 **Add local repository**로 열고, 변경 파일을 검토하여 커밋한 다음 **Publish repository**를 선택합니다. UI의 공개/비공개 선택을 반드시 확인하세요.

공식 안내: [로컬 코드 업로드](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)

## 5. 올리지 않는 것

`.gitignore`와 소스 ZIP 생성기는 DB·키·음원 디렉터리·환경 파일·가상환경·로그·생성 보고서를 제외합니다. 그러나 이름만 바꾼 비밀정보까지 자동 보장하는 도구는 아닙니다. `git diff --cached`도 직접 확인해야 합니다.

특히 `.gitignore`는 이미 추적 중인 파일을 자동 삭제하거나 Git 이력에서 지우지 않습니다. 실수로 키를 푸시했다면 공개 범위와 관계없이 키를 먼저 폐기/교체하고 별도로 이력 정리를 검토하세요. [보안 안내](../SECURITY.md)

## 6. Actions와 ZIP 생성

- `CI`: push/PR/수동 실행에서 서버 테스트, HTTP 스모크, Chromium 회귀 검사, Docker 빌드·기본 상태 확인을 하도록 정의했습니다.
- `Source archive`: Actions에서 수동 실행하면 저장소 소스 ZIP과 SHA-256 파일을 artifact로 제공합니다. GitHub Release 생성이나 서비스 배포는 자동 수행하지 않습니다.
- Dependabot: Python 의존성과 GitHub Actions 변경 제안을 만들도록 월간 설정했습니다. 자동 병합하지 않습니다.

로컬에서도 같은 소스 ZIP을 생성할 수 있습니다.

```bash
python scripts/build_previews.py
python scripts/package_release.py
```

결과는 무시되는 `dist/`에 생성됩니다. 같은 이름이 이미 있으면 검토 후 `--force`를 지정해야 합니다.

CI 파일이 있다는 사실이 CI 실행 성공을 뜻하지 않습니다. 첫 업로드 후 실제 Actions 결과를 확인하세요. GitHub Actions 정의는 읽기 전용 권한을 기본으로 사용하고 배포용 비밀 키를 요구하지 않습니다. 공개 이슈에 임의 서비스 키를 올리게 하지 마세요.

## 7. 라이선스와 공개 전 점검

프로젝트 라이선스는 미지정입니다. 저장소 소유자가 사용 목적에 맞게 선택한 뒤 LICENSE 파일과 README를 함께 갱신하세요. 이 ZIP은 라이선스를 임의로 결정하지 않습니다.

README의 이미지는 가상 데이터 화면입니다. 실제 운영 화면을 추가할 때에는 일정·위치·이메일·인증 코드·연결 QR을 가린 뒤 커밋하세요.

**GitHub 업로드는 상시 서버 호스팅이 아닙니다.** Windows PC를 끈 뒤에도 사용하려면 별도 상시 서버가 필요합니다. [배포 안내](DEPLOYMENT.md)
