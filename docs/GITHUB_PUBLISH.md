# GitHub 저장소 업로드

[문서 홈](../README.md)

이 ZIP을 풀면 `room-hub/`가 나옵니다. **그 안의 `README.md`, `app/`, `web/`, `widgets/`, `.github/`가 저장소 루트에 오도록** 올립니다. ZIP 파일 하나만 저장소에 커밋하지 않습니다.

## 최초 커밋 예시

```powershell
cd C:\Projects\room-hub

git init -b main
python scripts/check_repo.py

git add .
python scripts/check_repo.py --staged
git diff --cached --stat

git commit -m "Room Hub 0.1.4 source"
git remote add origin https://github.com/YOUR_GITHUB_ID/room-hub.git
git push -u origin main
```

이미 이력이 있는 원격 저장소는 clone한 작업 폴더에 현재 소스를 병합하고 일반 커밋으로 갱신하세요. 강제 push로 기존 이력을 지우지 않습니다.

## 절대 올리지 않는 것

- `data/`, SQLite DB, token
- `.env` 실제 파일
- Whisper/Qwen 모델과 `runtime/` 빌드 산출물
- TLS private key/CA private material
- 음성 원본, 전사/LLM 개인 기록, 백업 ZIP
- `.venv`, `.venv-v35`, Android 운영 로그

`.gitignore`와 `scripts/check_repo.py`는 보조 안전장치일 뿐입니다. 이미 Git이 추적 중인 비밀은 `.gitignore`만으로 제거되지 않으므로 커밋 전 `git diff --cached`를 직접 확인하세요.

## source archive 생성

```bash
python scripts/check_repo.py
python scripts/package_release.py
```

`dist/room_hub_github_v0.1.4.zip`과 SHA-256 파일이 생성됩니다. source archive에는 운영 데이터가 들어가지 않습니다.

## GitHub Actions

저장소에는 Python 테스트, HTTP/UI 회귀, Docker 빌드와 source archive 생성 워크플로가 포함되어 있습니다. ZIP을 만드는 과정에서 원격 GitHub Actions가 실행되었다는 뜻은 아니므로 최초 push 후 실제 결과를 확인하세요.

프로젝트 라이선스는 아직 미지정입니다. 공개 배포 전에 저장소 소유자가 라이선스를 선택하세요.
