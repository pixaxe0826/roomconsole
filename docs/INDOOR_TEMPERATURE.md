# 실내 온도·실내 공기 질 위젯 — Odyssey 통합 패치 v3

작성일: 2026-10-26, 한국시간 기준.

**이전 실내 온도 패치를 전혀 설치하지 않은 상태에서 이번 ZIP 하나로 설치합니다.** `RoomHub_Indoor_Sensors_20261026.zip`에는 실내 온도 위젯, 실내 공기 질 위젯, Odyssey의 센서 수집기와 설치기가 모두 들어 있습니다. 이전 v1·v2 패치를 먼저 적용할 필요가 없습니다.

Odyssey가 기존 NodeMCU의 **`http://192.168.0.22/api/sensors`**를 10초마다 한 번 읽고, 같은 응답을 두 위젯에 제공합니다. 이미 동작하는 NodeMCU의 펌웨어를 다시 업로드하거나 보드에 Room Hub 토큰을 넣을 필요가 없습니다.

운영 경로는 Room Hub `/opt/room-hub`, 데이터 `/var/lib/room-hub`, Python `/opt/room-hub/.venv/bin/python`, 서비스 `room-hub`와 `room-hub-https`를 기준으로 합니다. 접속 예시의 Odyssey 주소는 기존에 사용한 `192.168.0.15`이며, 현재 주소가 다르면 실제 주소를 사용하세요. **`192.168.0.22`는 센서 보드 주소**입니다. 이 패키지를 만든 환경에서 사용자 LAN에 접속하거나 Odyssey 운영 파일을 변경한 것은 아닙니다.

## 1. 새 API와 두 위젯의 역할

사용자가 제공한 응답 형식은 다음과 같습니다. 숫자는 설명용 예시이며 이 문서를 작성하면서 조회한 실측값이 아닙니다.

```json
{
  "temperature_c": 26.78,
  "humidity_percent": 40.13,
  "pressure_hpa": 1009.09,
  "dust_adc_raw": 26.0,
  "dust_a0_voltage_v": 0.0697,
  "dust_sensor_voltage_v": 0.2922
}
```

| API 필드 | 표시 위치와 의미 |
|---|---|
| `temperature_c` | **실내 온도**: API가 제공한 섭씨 온도 |
| `humidity_percent` | **실내 온도**: 상대습도, `%` |
| `pressure_hpa` | **실내 온도**: 기압, `hPa` |
| `dust_adc_raw` | **실내 공기 질**: 먼지 센서의 ADC 원시값. 소수 값 유지 |
| `dust_a0_voltage_v` | **실내 공기 질**: API가 제공한 A0 입력 전압, `V` |
| `dust_sensor_voltage_v` | **실내 공기 질**: API가 제공한 센서 출력 전압, `V` |

온도·습도·기압은 API의 해당 값을 그대로 사용합니다. 먼지 ADC를 온도로 해석하거나, 두 전압 중 하나를 다른 값으로 재계산하지 않습니다. `dust_adc_raw`는 단위 없는 ADC 값이며 평균값처럼 소수로 들어와도 저장과 표시에서 유지합니다. 전압은 소수점 네 자리로 표시합니다.

### 실내 온도

기본 크기는 **2×2**입니다. 예시 응답 기준으로 큰 **26.8°C**, **습도 40.1%**, **기압 1009.1 hPa**와 위치·조회 상태를 표시합니다. 확대하면 최근 1시간 온도 그래프, 최저·최고 온도와 습도 **40.13%**·기압 **1009.09 hPa**, 조회 시도·결과를 확인할 수 있습니다.

### 실내 공기 질

기본 크기는 **2×2**입니다. 예시 응답 기준으로 큰 **26.0 ADC**, **출력 0.2922 V**, **A0 0.0697 V**를 표시하며, 확대하면 최근 1시간 ADC 추세와 최저·최고, 조회 시도·결과를 확인할 수 있습니다. **`농도 미보정`**을 명시합니다.

현재 API에는 먼지 농도 보정식·기준값과 PM2.5·PM10·AQI가 없습니다. 따라서 이 위젯은 ADC 추세를 보여 주며, `좋음/나쁨`이나 `µg/m³` 농도를 임의로 만들지 않습니다. 온도 위젯의 기압도 해면기압으로 변환하지 않고 받은 값을 그대로 표시합니다.

### 조회 시각의 의미

이 여섯 필드에는 센서의 실제 측정 시각이나 센서별 상태 정보가 없습니다. 화면의 **성공 조회 시각**은 Odyssey가 유효한 HTTP 응답을 받은 시각입니다. NodeMCU가 내부에 남아 있는 같은 값을 반복 반환해도 이 형식만으로는 오래된 측정을 판별할 수 없습니다. 표시 자릿수 역시 센서 정확도를 보증하지 않습니다.

| 구간 | 동작 |
|---|---|
| Odyssey → NodeMCU | `GET http://192.168.0.22/api/sensors`, 백그라운드 10초 주기 |
| 온도 위젯 → Odyssey | 기존 HTTPS의 `GET /api/indoor-temperature/room-temp-01` |
| 공기 질 위젯 → Odyssey | 기존 HTTPS의 `GET /api/indoor-air-quality/room-temp-01` |

두 위젯은 같은 `device_id`를 사용하므로 NodeMCU에 센서를 두 번 등록하지 않습니다. 보드 조회는 Odyssey 수집기 한 곳에서 수행하고, 브라우저는 저장된 결과를 읽습니다. 기존 HTTPS 화면에서 NodeMCU HTTP로 직접 접속하지 않으므로 브라우저의 HTTP 예외나 CORS 설정을 추가할 필요가 없습니다. 이 수집·표시 기능은 Windows LLM/Whisper를 사용하지 않습니다.

## 2. 제공 파일과 설치 범위

이번 파일은 **`RoomHub_Indoor_Sensors_20261026.zip`**입니다. 새 폴더에 풀어 사용하세요.

| 항목 | 용도 |
|---|---|
| `ODYSSEY_GUIDE.md` | 이 설치·설정·복구 안내 |
| `update/apply_update.py` | 기존 Room Hub에 통합 기능을 넣는 설치기 |
| `update/PATCH_MANIFEST.json` | 패치 ID와 설치 대상 파일 정보 |
| `update/payload/` | 센서 수집기, 원본 설정 도구, 위젯 두 종류 |
| `preview/indoor_temperature_preview.html` | 두 위젯의 독립 디자인 미리보기 |
| `source/feature.patch` | 검토·저장소 반영용 변경 diff |
| `VALIDATION.md` | 수행한 검증과 실기기 확인 범위 |

미리보기의 값·시각·그래프는 **예시 데이터**이며 실제 보드에 요청하지 않습니다. 펌웨어는 포함하지 않습니다. `source/feature.patch`는 검토용이고, 운영 서버에는 아래 설치기를 사용합니다.

설치 대상은 다음 **파일 8개와 `app/main.py`의 연결 두 줄**입니다.

- `app/indoor_temperature.py`
- `scripts/configure_temperature_sensor.py`
- `widgets/indoor-temperature/manifest.json`
- `widgets/indoor-temperature/widget.js`
- `widgets/indoor-temperature/style.css`
- `widgets/indoor-air-quality/manifest.json`
- `widgets/indoor-air-quality/widget.js`
- `widgets/indoor-air-quality/style.css`

`main.py`에는 import와 경로 등록을 넣습니다. `app/store.py`는 설치 대상이 아니므로 운영 중인 ClosingConnection 수정이 유지됩니다. 기존 설정·레이아웃·메모·할 일·타이머·AI/ASR 설정은 백업 후 그대로 사용합니다. 확인된 통합 지점과 다르거나 대상 파일에 알 수 없는 수정이 있으면 설치기가 중단합니다. 이 경우 전체 폴더 덮어쓰기나 `git reset --hard`로 우회하지 말고 출력된 충돌을 확인하세요.

적용 중 Room Hub가 잠깐 중단되므로 실행 중인 LLM·전사 작업이 끝난 뒤 진행합니다. 기존 `room-hub-https`나 Windows 연결 서비스의 재설정은 필요하지 않습니다.

## 3. Odyssey에서 새 API 연결 확인

평소 사용하는 SSH 사용자명으로 Odyssey에 접속합니다. 아래 명령의 `실제_SSH_사용자명`과 Odyssey IP는 현재 접속 정보로 바꾸세요.

```powershell
ssh 실제_SSH_사용자명@192.168.0.15
```

**Odyssey 터미널에서** 실행합니다.

```bash
curl --noproxy '*' --connect-timeout 3 --max-time 5 -fsS \
  http://192.168.0.22/api/sensors
```

1절의 여섯 필드가 JSON 숫자로 반환되어야 합니다. 값 자체가 예시와 같을 필요는 없습니다. PC에서 API가 열리는 것만으로 Odyssey에서도 접근 가능하다는 뜻은 아니므로 이 확인은 Odyssey에서 진행합니다.

연결에 실패하면 보드 전원·Wi-Fi, IP 주소, 같은 LAN에서의 접근을 확인하세요. 공유기에서 NodeMCU에 현재 DHCP 주소를 예약하면 재접속 후 IP 변경을 줄일 수 있습니다.

설정 도구는 HTTP, 명시적인 사설 IPv4, 지정된 센서 경로를 사용합니다. 새 통합 API 경로는 `/api/sensors`입니다. 호스트명, URL 사용자 정보·query·fragment, redirect는 지원하지 않고 수집기는 환경 프록시를 사용하지 않습니다. 이전 `/api/temperature` 형식의 호환 기능도 남아 있으나 **이번 설치에서는 `/api/sensors`를 등록해야 두 위젯에 모두 데이터가 표시됩니다.**

## 4. 이번 패치 하나로 설치

### 4-1. ZIP 복사와 새 폴더에 압축 해제

Windows PowerShell에서 ZIP을 다운로드한 폴더로 이동한 뒤 실행합니다.

```powershell
scp .\RoomHub_Indoor_Sensors_20261026.zip 실제_SSH_사용자명@192.168.0.15:~/
ssh 실제_SSH_사용자명@192.168.0.15
```

이후 Odyssey 터미널에서 실행합니다. 이미 같은 폴더가 있으면 비어 있는 다른 새 폴더를 사용하세요.

```bash
mkdir "$HOME/room-hub-sensors-20261026"
unzip "$HOME/RoomHub_Indoor_Sensors_20261026.zip" \
  -d "$HOME/room-hub-sensors-20261026"
cd "$HOME/room-hub-sensors-20261026"
python3 --version
python3 -c 'import json; print(json.load(open("update/PATCH_MANIFEST.json"))["patch_id"])'
python3 update/apply_update.py --target /opt/room-hub --check
```

Python 3.11 이상을 사용합니다. 설치기에 별도 pip 설치는 필요 없습니다. 패치 ID는 **`room-hub-indoor-sensors-v3`**이어야 합니다.

`READY: ...; no files changed`가 나오면 적용할 준비가 된 것입니다. `ALREADY APPLIED`는 같은 v3 소스가 이미 설치된 상태입니다. `STOP`이 나오면 출력된 충돌을 확인한 뒤 진행합니다. 파일 해시는 손상 여부 확인용이며 서명은 아닙니다.

### 4-2. 데이터 백업 → 소스 설치 → 센서 원본 등록

방금 압축을 푼 폴더에서 다음 블록을 실행합니다. Room Hub 서비스를 중지하고 데이터 백업을 만든 뒤 소스를 적용합니다. 이어 **서비스와 같은 OS 사용자**로 원본을 등록하므로 새 센서 SQLite 파일의 소유권도 맞습니다. `trap`은 블록 종료 시 서비스를 다시 시작합니다.

```bash
(
  set -e
  roomhub_update="$PWD/update/apply_update.py"
  roomhub_backup="/var/backups/room-hub/pre-sensors-v3-$(date -u +%Y%m%dT%H%M%SZ)"
  roomhub_service_user=$(systemctl show room-hub -p User --value)
  if [ -z "$roomhub_service_user" ]; then
    roomhub_service_user=root
  fi
  sudo -v
  sudo install -d -m 700 "$roomhub_backup"
  sudo systemctl stop room-hub
  trap 'sudo systemctl start room-hub' EXIT
  sudo tar -C /var/lib -czf "$roomhub_backup/data.tgz" room-hub
  sudo chmod 600 "$roomhub_backup/data.tgz"
  sudo python3 "$roomhub_update" \
    --target /opt/room-hub --apply --server-stopped
  sudo -u "$roomhub_service_user" /opt/room-hub/.venv/bin/python \
    /opt/room-hub/scripts/configure_temperature_sensor.py \
    --data-dir /var/lib/room-hub set-source \
    --device-id room-temp-01 \
    --url http://192.168.0.22/api/sensors --label '내 방'
)
```

`room-temp-01`은 두 위젯이 함께 사용하는 센서의 논리 ID입니다. 이름에 `temp`가 있어도 이번에는 온도·습도·기압·먼지 데이터를 모두 담습니다. 공기 질용으로 같은 보드를 한 번 더 등록하지 않습니다.

적용 출력의 **`BACKUP: /opt/room-hub-update-backups/...` 경로를 기록**하세요. 이것은 소스 복구용 백업이며 `/var/backups/room-hub/`의 `data.tgz`와 별개입니다. 같은 v3가 이미 적용되어 소스 변경이 없으면 새 소스 백업을 만들지 않습니다. 설치기는 기본 8088 포트가 닫혔는지 확인합니다. Room Hub의 운영 포트가 다른 경우 설치기의 `--port`에 실제 포트를 지정합니다.

### 4-3. 서비스 재시작 결과 확인

```bash
sudo systemctl is-active room-hub
curl --noproxy '*' -fsS http://127.0.0.1:8088/healthz
```

재시작 직후라면 잠깐 기다렸다가 health 확인을 다시 합니다. 계속 실패하면 로그를 확인합니다.

```bash
sudo journalctl -u room-hub -n 60 --no-pager
```

이번 기능은 기존 Room Hub Python 의존성을 사용합니다. 운영 pip 패키지를 일괄 업그레이드할 필요가 없습니다.

## 5. 수집 상태 확인

서비스 실행 후 약 10초 뒤 다음을 실행합니다.

```bash
roomhub_service_user=$(systemctl show room-hub -p User --value)
if [ -z "$roomhub_service_user" ]; then
  roomhub_service_user=root
fi
sudo -u "$roomhub_service_user" /opt/room-hub/.venv/bin/python \
  /opt/room-hub/scripts/configure_temperature_sensor.py \
  --data-dir /var/lib/room-hub list
```

`room-temp-01`에서 다음을 확인합니다.

- `acquisition`: `http_pull`
- `source_kind`: `environment`
- `source_url`: `http://192.168.0.22/api/sensors`
- `source_status`: 정상 조회 후 `ok`
- `source_error`: 정상 조회 후 `null`
- `received_at`: 최근 정상 응답을 검증한 시각

`last_attempt_at`은 최근 조회 시도가 끝난 시각입니다. 오류가 발생해도 `received_at`과 마지막 정상 값은 갱신되지 않습니다. 시각 문자열에 `+00:00`이 붙으면 UTC이며, 한국시간은 여기에 9시간을 더한 시각입니다.

아직 위젯을 화면에 추가하지 않아도 Odyssey 서비스는 등록된 센서의 수집을 시작합니다. 원본 목록만 정상이고 화면에 값이 없다면 다음 절의 위젯 추가와 표시 기기 새로고침을 확인합니다.

## 6. 관리자에서 두 위젯 배치

1. 평소 쓰는 관리자 주소에 접속합니다. 기존 LAN 주소 기준 예시는 `https://192.168.0.15:8443/manager`입니다.
2. **위젯과 배치 → 위젯 라이브러리 → 다시 검색**을 누릅니다.
3. **실내 온도**와 **실내 공기 질**을 각각 추가합니다. 기본 크기는 각각 **2×2**입니다. 공간이 부족하면 기존 위젯 위치나 크기를 조정합니다.
4. **두 위젯 모두** 아래 설정을 사용합니다.

```json
{
  "device_id": "room-temp-01",
  "location": "내 방"
}
```

5. **화면에 적용**을 눌러 배치를 저장하고, iPad 표시 화면도 새로고침합니다.
6. 온도 위젯에 온도·습도·기압이, 공기 질 위젯에 먼지 ADC·전압 정보가 표시되는지 확인합니다. 각 위젯을 눌러 확대 화면과 성공 조회 시각도 확인합니다.

처음 설치한 직후 그래프에 점이 적은 것은 정상입니다. 수집을 시작한 뒤의 기록만 쌓이며 과거 한 시간의 값을 만들어 넣지 않습니다.

위젯 설정에는 NodeMCU URL이나 토큰을 넣지 않습니다. URL은 Odyssey의 `set-source`로 관리합니다. CLI의 `--label`과 화면의 `location`은 별도이므로 화면에 보일 이름은 위젯 설정에서 바꿉니다. 같은 센서를 여러 위젯에서 표시할 수 있고, 다른 보드를 추가할 때만 서로 다른 `device_id`로 등록합니다. 장치 수 상한은 16개입니다.

표시 화면은 기존에 페어링한 기기여야 하며, **요청하는 종류의 위젯**이 해당 ID로 저장된 배치에 있어야 합니다. 온도 위젯만 배치했다고 공기 질 표시 API도 열리는 것은 아닙니다. 관리자는 기존 관리자 인증으로 확인할 수 있습니다. 이번 패치는 AI 명령 실행 권한을 추가하지 않습니다.

## 7. 갱신 상태·이력·문제 해결

### 실패와 복구 표시

| 조건 | 화면 표시 |
|---|---|
| 첫 정상 응답 전, 오류 없음 | 값 대신 `—`, 첫 조회 대기 |
| 정상 기록 없이 조회 실패 | 값 대신 `—`, 조회 실패 |
| 정상 조회, 마지막 성공 후 30초 미만 | 현재 값과 조회 정상 |
| 조회 실패, 마지막 성공 후 30초 미만 | 마지막 정상 값과 조회 실패 |
| 마지막 성공 후 30초 이상, 120초 미만 | 마지막 정상 값과 조회 지연 |
| 마지막 성공 후 120초 이상 | 마지막 정상 값과 연결 끊김 |
| 다시 정상 응답 수신 | 새 값·성공 시각으로 갱신, 정상 상태 복귀 |

통신 실패나 유효하지 않은 응답은 마지막 정상 값을 새 값처럼 갱신하지 않습니다. 여섯 필드를 하나의 응답으로 검증하므로, 필수 값 하나가 빠지거나 유효하지 않으면 해당 응답 전체를 저장하지 않고 두 위젯 모두 마지막 정상 응답을 유지합니다. `0°C`, 습도 `0%`, 먼지 ADC `0`은 각각 허용 범위에 있는 실제 숫자로 처리하며 빈 값과 구분합니다. 표시 권한이 사라지면 화면에 남아 있던 숫자도 지웁니다. 그래프는 수집이 끊긴 구간을 이어 그리지 않습니다.

기본 수집 주기는 10초입니다. HTTP timeout은 3초이며 전체 요청에는 4초 상한을 둡니다. 여러 센서 지연이나 저장소 처리 시간에 따라 실제 간격이 길어질 수 있습니다. 표시 화면도 10초마다 Odyssey를 읽으므로 화면 반영에는 두 조회 주기와 통신 시간이 함께 영향을 줍니다. 같은 종류의 기본 카드와 확대 화면은 표시 조회를 공유합니다.

원시 이력은 최근 24시간을 기준으로 **다음 정상 수집 시** 오래된 행을 정리합니다. 수집이 중지되면 오래된 행이 DB에 남을 수 있습니다. 화면 그래프는 최근 1시간의 1분 단위 대표값을 사용합니다. 저장소 파일은 기존 데이터 폴더 안의 별도 `indoor-temperature.sqlite3`이며, 이번 버전에서도 같은 파일을 사용합니다.

### 주요 점검

| 상황 | 확인할 내용 |
|---|---|
| 첫 조회 대기가 계속됨 | Odyssey에서 3절의 `curl`, 원본 목록의 URL·enabled·오류 확인 |
| 조회 실패·조회 지연 | NodeMCU 전원·Wi-Fi·주소·HTTP 응답 확인 |
| 온도만 나오고 습도·기압·먼지가 없음 | `source_url`이 새 `/api/sensors`인지 확인 |
| 응답 형식 오류 | 여섯 필드 모두 JSON 숫자인지 확인. 문자열·boolean·null·NaN·Infinity는 정상 응답이 아님 |
| 같은 값이 반복됨 | API에 측정 시각이 없으므로 보드 자체에서 최신 측정 여부 확인 |
| 표시 API 401·403 | 표시 기기 페어링과 해당 종류 위젯의 저장된 배치 확인 |
| 표시 API 404 | v3 설치와 Room Hub 재시작, 위젯 다시 검색·화면 새로고침 확인 |
| Odyssey 재시작 후 | 보존된 마지막 상태에서 다음 정상 수집 후 회복하는지 확인 |

실제 장비에서는 정상 조회, 보드 전원 끄기, 30초·120초 상태 변화, 다시 켠 뒤 복구, iPad 가로·세로와 확대 화면을 확인하세요. 온도·습도·기압의 정확도와 먼지 센서 농도 보정은 별도 측정·검증 사항입니다. 예시 데이터와 합성 테스트는 실제 LAN 연결이나 센서 보정을 대신하지 않습니다.

### 센서 IP가 바뀐 경우

먼저 바뀐 주소에서 3절의 `curl`로 응답을 확인합니다. 이후 아래의 `새_사설IPv4`를 실제 IP로 바꿔 **같은 서비스 사용자**로 실행합니다.

```bash
sudo -u "$roomhub_service_user" /opt/room-hub/.venv/bin/python \
  /opt/room-hub/scripts/configure_temperature_sensor.py \
  --data-dir /var/lib/room-hub set-source \
  --device-id room-temp-01 \
  --url http://새_사설IPv4/api/sensors --label '내 방'
```

새 SSH 세션이라면 먼저 5절에서 `roomhub_service_user`를 구하는 부분을 실행합니다. 실행 중인 서비스는 다음 수집 주기에서 새 설정을 읽으므로 소스 재설치나 재시작은 필요하지 않습니다.

같은 ID와 URL을 다시 설정하면 중복 등록되지 않고 기존 이력을 유지합니다. URL을 바꾸면 현재 표시를 비우고 새 원본의 첫 정상 응답을 기다립니다. 이전 이력은 DB에 보존하지만 화면 그래프와 최저·최고는 현재 원본 설정 이후의 기록만 사용하므로 두 주소의 값이 섞이지 않습니다.

## 8. 소스 롤백

이번 기능을 제거하려면 먼저 관리자에서 **실내 온도와 실내 공기 질 위젯을 제거하고 배치를 저장**합니다. 이후 이번 설치 출력의 실제 `BACKUP` 경로를 사용합니다. 이전 패치를 설치하지 않은 상태에서 설치했다면 아래 롤백은 이번에 추가한 소스 파일과 `main.py`의 연결 두 줄을 설치 전 상태로 되돌립니다.

압축 해제 폴더에서 실행합니다.

```bash
roomhub_source_backup='/opt/room-hub-update-backups/실제_백업_폴더'
(
  set -e
  sudo systemctl stop room-hub
  trap 'sudo systemctl start room-hub' EXIT
  sudo python3 update/apply_update.py \
    --target /opt/room-hub \
    --rollback "$roomhub_source_backup" --server-stopped
)
curl --noproxy '*' -fsS http://127.0.0.1:8088/healthz
```

재시작 직후 health 확인이 실패하면 잠깐 기다렸다가 다시 확인합니다. 롤백은 해당 업데이트의 소스만 복구합니다. 센서 DB·원본 설정·이력은 남으며 기존 Room Hub 데이터를 자동으로 복원하거나 덮어쓰지 않습니다. `app/store.py`의 기존 수정도 복구 대상이 아닙니다. 적용 후 파일을 직접 편집했다면 롤백이 충돌을 보고하므로 백업을 보존하고 해당 파일을 확인하세요.

설치기는 알려진 이전 온도 패치에서의 업데이트도 지원하지만 **이번 사용 절차에는 이전 패치가 필요 없습니다.** 과거 버전의 설치까지 따로 되돌리는 경우에는 그 당시 패키지의 설치기와 백업을 사용해야 합니다. 이번 v3 설치기에는 이번 v3가 출력한 백업을 지정합니다.

검증 범위와 결과는 패키지의 `VALIDATION.md`를 확인하세요. 실제 NodeMCU 응답·Odyssey LAN 접근·iPad Safari 표시는 해당 장비에서 확인해야 합니다.
