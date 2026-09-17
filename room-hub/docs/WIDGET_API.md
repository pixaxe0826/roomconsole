# 폴더형 위젯 API v1

## 폴더 구조

```text
widgets/my-widget/
  manifest.json
  widget.js
  style.css
```

```json
{
  "id": "my-widget",
  "name": "새 위젯",
  "description": "서버 데이터의 내용을 표시합니다.",
  "icon": "note",
  "apiVersion": 1,
  "version": "1.0.0",
  "entry": "widget.js",
  "style": "style.css",
  "defaultSize": {"w": 2, "h": 2},
  "configDefaults": {"caption": "정보"}
}
```

id와 폴더명은 동일한 소문자 slug이며 `[a-z][a-z0-9_-]{0,63}` 규칙입니다. 엔트리 파일은 같은 폴더의 파일이어야 하며 symlink는 허용하지 않습니다. 폴더의 Python 파일을 자동 실행하는 구조가 아닙니다.

## 렌더링

```javascript
export function render(ctx) {
  const {esc, icon} = ctx.util;
  const data = ctx.state.widget_data[ctx.instance.type] || {};
  return `<div class="widget-content ${ctx.compact ? 'compact' : ''}">
    <div class="widget-head"><h2>${icon('note')}
      ${esc(ctx.instance.title || '새 위젯')}</h2></div>
    <p>${esc(data.message || '데이터 대기 중')}</p>
  </div>`;
}

export function bind(root, ctx) {
  // Optional: attach navigation or permitted completion events here.
  // Return cleanup for every listener/timer you create.
  return () => {};
}
```

| ctx | 의미 |
|---|---|
| state | 서버 스냅샷: settings, layout, tasks, weather, widgets, widget_data, today, revision |
| instance | 이 위젯의 id/type/title/x/y/w/h/config |
| selectedDate | 현재 조회 날짜 YYYY-MM-DD |
| month | 현재 조회 월 YYYY-MM |
| expanded | 전체 화면 표시인지 |
| compact | 작아 요약 UI를 사용해야 하는지 |
| now | 서버 시각 보정을 적용한 Date |
| util | esc, icon, prettyDate, dateParts, sortedTasks 등 Room 유틸리티 |
| selectDate(date) | 조회 날짜 변경. 데이터 수정 아님 |
| changeMonth(value) | '-1', '1', 'today' |
| expand() | 현재 위젯 확대 |
| openWidget(id) | 다른 위젯 확대 |
| canCompleteTasks | 연결·권한·서버 기능을 확인한 완료 변경 가능 여부 |
| taskPending(id) | 해당 회차 저장 요청 처리 중 여부 |
| setTaskCompletion(id, completed) | 완료 전용 서버 API 호출. completed는 boolean |

`render`는 HTML 문자열을 반환하고 `bind`는 선택적 정리 함수를 반환합니다. 외부 문자열을 반드시 `esc` 처리합니다. 화면 확대·재렌더링 때 기존 bind 정리 함수를 호출합니다. 현재 계약에는 shadow DOM/sandbox 격리가 없습니다. CSS는 자신의 위젯 클래스 범위로 작성하여 다른 위젯을 침범하지 않게 하세요.

클라이언트 입력 금지 정책을 유지하려면 input, textarea, select, contenteditable을 만들지 마세요. 터치 날짜 이동 등 읽기 전용 조작에 `data-stop`을 붙이면 위젯 전체 확대 이벤트와 충돌하지 않습니다. 0.1.1에서는 할 일 완료·완료 취소만 `ctx.setTaskCompletion`으로 허용합니다. 버튼에는 `data-stop`을 붙여 확대와 충돌하지 않게 하세요. 제목/날짜 수정, 생성, 삭제는 표시 세션에서 거부됩니다.

## 서버 데이터 주입

```http
PUT /api/widgets/my-widget/data
Authorization: Bearer <ADMIN_KEY>
Content-Type: application/json

{"data":{"message":"서버에서 도착한 새 정보"}}
```

서버가 저장하고 WebSocket으로 갱신을 알립니다. 다음 스냅샷의 `widget_data['my-widget']`로 전달됩니다. 종류별 공유 데이터이며, 같은 종류의 여러 인스턴스는 `instance.config`의 키나 하위 경로로 데이터를 나누어 읽을 수 있습니다. JSON 전체 크기 100KB 제한입니다.

센서·별도 API가 필요한 위젯은 검토된 수집 프로그램을 서버에서 명시적으로 실행하고 위 API로 데이터를 넣으세요. 폴더를 넣는 것만으로 임의 서버 코드를 자동 실행하지 않는 것은 의도된 보안 경계입니다.

## 설치 및 갱신

폴더 복사 → 관리자 `위젯과 배치` → `다시 검색` → 위젯 추가 → 설정·크기·위치 편집 → `화면에 적용`.

코드를 변경하면 manifest의 version도 올리세요. 동적 모듈 캐시를 새 URL로 구분합니다. 동일 파일명의 CSS를 크게 변경했다면 화면 새로고침도 진행하세요. 제거된 위젯을 사용하는 배치는 관리자에서 정리해야 합니다. 잘못된 manifest는 검색 오류로 표시됩니다.

## 신뢰 경계

위젯은 완전히 신뢰하는 코드만 설치해야 합니다. 관리자 미리보기에서도 같은 출처 권한으로 실행되며, sandbox가 아닙니다. `widgets/` 아래 파일은 정적으로 제공되므로 키·토큰·비공개 설정 파일·수집 프로그램의 비밀값을 넣지 마세요. 브라우저 기반 ZIP 업로드/설치 기능은 제공하지 않습니다.
