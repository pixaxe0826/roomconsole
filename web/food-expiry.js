/* Manager-only food editing. Display widgets never load this editor or call CRUD. */
(() => {
'use strict';
const R = window.Room, E = R.esc, DEMO = Boolean(window.ROOM_DEMO);
let root = null, items = [], draft = {}, busy = false, epoch = 0, loading = null;
let requestKey = null, stale = false, loadFailed = false;
let listClock = null;

function newKey() {return window.crypto?.randomUUID?.() || `food-${Date.now()}-${Math.random().toString(36).slice(2)}`;}
function active() {return root?.isConnected;}
function feedback(message, error = false) {
  const node = root?.querySelector('[data-food-feedback]');
  if (node) {node.textContent = message; node.classList.toggle('food-feedback-error', error);}
}
async function api(path, method = 'GET', body) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {method, credentials:'same-origin',
      headers:{'X-Room-Request':'1', 'Content-Type':'application/json'},
      body:body === undefined ? undefined : JSON.stringify(body), signal:controller.signal});
    if (!response.ok) {
      let payload = {}; try {payload = await response.json();} catch {}
      const text = response.status === 401 ? '관리자 로그인이 필요합니다. 다시 로그인해 주세요.'
        : typeof payload.detail === 'string' ? payload.detail
          : Array.isArray(payload.detail) ? payload.detail.map(x => x.msg).join(' · ') : `서버 오류 ${response.status}`;
      const error = new Error(text); error.status = response.status; throw error;
    }
    return response.status === 204 ? null : await response.json();
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(method === 'GET'
      ? '목록을 불러오지 못했습니다. 새로고침해 주세요.'
      : '저장 결과를 확인하지 못했습니다. 목록을 새로 확인한 뒤 다시 시도하세요. 입력 내용과 요청 번호는 유지됩니다.');
    throw error;
  } finally {clearTimeout(timer);}
}
function shell() {
  return `<div class="food-manager"><div class="food-manager-banner"><strong>식품과 날짜를 한눈에.</strong><p>직접 입력하거나 iPad 마이크로 식품·개수·소비기한을 말하세요. 음성 변경은 관리자 LLM 인터페이스의 Personal Agent에서 확인 후 저장합니다.</p><p class="food-voice-example">예: “소비기한 알리미에 우유 2개, 소비기한 11월 20일로 추가해 줘.”</p><p>iPad에서 보려면 <a href="#layout">위젯과 배치</a>에서 ‘소비기한 알리미’를 추가하고 화면에 적용하세요. 배치하면 등록된 전체 식품 목록을 연결된 화면에 공유합니다.</p></div>
    <div class="food-manager-workspace"><section class="panel food-manager-list-panel"><div class="panel-head"><div><h2>등록한 식품 <span data-food-count></span></h2><p>소비기한순 · 같은 식품도 날짜별로 따로 보관합니다.</p></div><button type="button" class="btn small" data-food-op="refresh">새로고침</button></div><div class="food-manager-list" data-food-list tabindex="0" role="region" aria-label="등록한 식품 목록">불러오는 중…</div></section>
    <section class="panel food-manager-editor"><div class="panel-head"><h2 data-food-editor-title>새 식품</h2><button type="button" class="btn small" data-food-op="new">새로 작성</button></div><div data-food-form></div><p class="food-manager-feedback" data-food-feedback role="status" aria-live="polite"></p></section></div>
    <p class="food-manager-caveat">입력한 소비기한을 기준으로 지난 날짜·오늘·3일 이내를 구분합니다. 항목을 자동 삭제하거나 잠금 화면 알림·소리를 보내지 않습니다.</p></div>`;
}
function collect() {
  const form = root?.querySelector('form'); if (!form) return;
  const fields = form.elements;
  draft.name = fields.name.value; draft.quantity = fields.quantity.value; draft.expiry_date = fields.expiry_date.value;
}
function setBusy() {
  if (!active()) return;
  for (const element of root.querySelectorAll('button,input')) element.disabled = busy;
  if (stale) for (const element of root.querySelectorAll('[type=submit],[data-food-op=delete]')) element.disabled = true;
  const form = root.querySelector('form'); if (form) form.setAttribute('aria-busy', String(busy));
}
function paintForm() {
  if (!active()) return;
  root.querySelector('[data-food-editor-title]').textContent = draft.id ? '식품 수정' : '새 식품';
  root.querySelector('[data-food-form]').innerHTML = `<form class="stack" data-food-editor-form>
    <label>식품<input name="name" required maxlength="120" autocomplete="off" placeholder="예: 우유" value="${E(draft.name || '')}"></label>
    <div class="food-manager-form-row"><label>개수<input name="quantity" type="number" inputmode="numeric" min="1" max="9999" step="1" required placeholder="예: 2" value="${E(draft.quantity ?? '')}"></label><label>소비기한<input name="expiry_date" type="date" required value="${E(draft.expiry_date || '')}"></label></div>
    <p class="field-help">식품 포장에 적힌 날짜를 입력하세요. 개수는 1~9,999 사이의 정수입니다. 날짜가 지난 항목도 기록할 수 있습니다.</p>
    <div class="food-manager-conflict ${stale ? '' : 'hidden'}" data-food-conflict role="status">서버의 항목이 변경되었거나 삭제되었습니다. 입력한 값은 남겨 두었습니다. 저장 전에 최신 값을 다시 불러와 확인하세요.<button type="button" class="btn small" data-food-op="reload-item">서버 값 다시 불러오기</button></div>
    <div class="form-actions"><button class="btn primary" type="submit">${draft.id ? '수정 저장' : '식품 추가'}</button>${draft.id ? '<button class="btn danger" type="button" data-food-op="delete">삭제</button>' : ''}</div></form>`;
  const form = root.querySelector('form'); form.oninput = collect; form.onchange = collect;
  form.onsubmit = save; setBusy();
}
function managerClock() {
  const state = window.RoomManager?.getState();
  return {today:state?.today || state?.food?.today,
    timezone:state?.settings?.timezone || state?.food?.timezone};
}
function dayStatus(item) {
  const manager = managerClock();
  // A food-only refresh can cross midnight before the manager snapshot does.
  // Use its date until a subsequent manager date/timezone change supersedes it.
  const managerChanged = listClock && (manager.today !== listClock.manager.today
    || manager.timezone !== listClock.manager.timezone);
  const today = managerChanged && manager.today ? manager.today : listClock?.today || manager.today;
  const days = Math.round((Date.parse(item.expiry_date + 'T00:00:00Z') - Date.parse(today + 'T00:00:00Z')) / 86400000);
  if (!Number.isFinite(days)) return ['unknown', '날짜 확인 필요'];
  return days < 0 ? ['expired', `기한 ${-days}일 지남`] : days === 0 ? ['today', '오늘까지']
    : days <= 3 ? ['soon', `${days}일 남음`] : ['later', `${days}일 남음`];
}
function paintList() {
  if (!active()) return;
  const list = root.querySelector('[data-food-list]'), top = list.scrollTop;
  const sorted = [...items].sort((a, b) => String(a.expiry_date).localeCompare(String(b.expiry_date))
    || String(a.name).localeCompare(String(b.name), 'ko') || String(a.id).localeCompare(String(b.id)));
  root.querySelector('[data-food-count]').textContent = `(${sorted.length}종)`;
  list.innerHTML = `<table class="food-manager-table"><caption>식품명 버튼을 누르면 수정합니다.</caption><colgroup><col class="food-manager-name-col"><col class="food-manager-count-col"><col class="food-manager-date-col"></colgroup><thead><tr><th scope="col">식품</th><th scope="col">개수</th><th scope="col">소비기한</th></tr></thead><tbody>${sorted.map(item => {
    const [kind, text] = dayStatus(item);
    return `<tr class="${draft.id === item.id ? 'selected' : ''}"><th scope="row"><button type="button" data-food-op="edit" data-id="${E(item.id)}" aria-label="${E(item.name)} · ${E(item.expiry_date)} 수정">${E(item.name)} ${R.icon('edit', 12)}</button></th><td>${E(item.quantity)}개</td><td><time datetime="${E(item.expiry_date)}">${E(item.expiry_date)}</time><span class="food-manager-date-status ${kind}">${E(text)}</span></td></tr>`;
  }).join('') || `<tr><td colspan="3" class="food-manager-empty">${loadFailed ? '목록을 불러오지 못했습니다. 새로고침해 주세요.' : '등록한 식품이 없습니다.'}</td></tr>`}</tbody></table>`;
  list.scrollTop = top;
  if (draft.id) {
    const current = items.find(x => x.id === draft.id);
    if (!current || current.version !== draft.version) stale = true;
  }
  const warning = root.querySelector('[data-food-conflict]'); if (warning) warning.classList.toggle('hidden', !stale);
  setBusy();
}
async function refresh() {
  if (!active()) return;
  if (loading) return loading;
  const generation = epoch, manager = managerClock();
  const work = (async () => {
    try {
      const result = DEMO ? {items:window.ROOM_DEMO.food_demo?.items || window.ROOM_DEMO.food?.items || []} : await api('/api/food/items');
      if (generation !== epoch || !active()) return;
      if (!Array.isArray(result?.items)) throw new Error('서버 목록 형식이 올바르지 않습니다.');
      items = result.items; listClock = {today:result.today, manager}; loadFailed = false; paintList();
    } catch (error) {
      if (generation !== epoch || !active()) return;
      if (error.status === 401) {reset(); R.toast(error.message); return;}
      loadFailed = true; paintList(); feedback(error.message, true);
    }
  })();
  loading = work;
  try {await work;} finally {if (loading === work) loading = null;}
}
async function save(event) {
  event.preventDefault(); const form = event.currentTarget;
  if (busy || stale || !form.reportValidity()) return;
  collect(); const value = draft, generation = epoch;
  const quantity = Number(value.quantity);
  if (!value.name.trim() || !Number.isInteger(quantity) || quantity < 1 || quantity > 9999) {
    feedback('식품명과 1~9,999 사이의 정수 개수를 입력하세요.', true); return;
  }
  const body = {name:value.name.trim(), quantity, expiry_date:value.expiry_date};
  if (value.id) body.version = value.version;
  else {
    const signature = JSON.stringify(body);
    if (requestKey?.signature !== signature) requestKey = {signature, id:newKey()};
    body.request_id = requestKey.id;
  }
  busy = true; setBusy(); feedback('저장 중…');
  try {
    if (DEMO) throw new Error('미리보기는 합성 예시입니다. 실제 저장은 실행 중인 서버의 관리자에서 하세요.');
    await api('/api/food/items' + (value.id ? '/' + encodeURIComponent(value.id) : ''), value.id ? 'PUT' : 'POST', body);
    if (generation !== epoch) return;
    draft = {}; stale = false; requestKey = null; paintForm(); feedback('서버에 저장했습니다.');
    if (loading) await loading;
    await refresh(); window.RoomManager?.refresh(false);
  } catch (error) {
    if (generation !== epoch) return;
    if (error.status === 401) {reset(); R.toast(error.message); return;}
    if (value.id && [404, 409].includes(error.status)) {stale = true; paintForm(); await refresh();}
    feedback(error.message, true);
  } finally {if (generation === epoch) {busy = false; setBusy();}}
}
async function remove() {
  const value = draft, generation = epoch;
  if (busy || stale || !value.id || !window.confirm(`「${value.name}」 항목을 삭제할까요?`)) return;
  busy = true; setBusy(); feedback('삭제 중…');
  try {
    if (DEMO) throw new Error('미리보기에서는 실제 데이터를 변경하지 않습니다.');
    await api('/api/food/items/' + encodeURIComponent(value.id) + '?version=' + encodeURIComponent(value.version), 'DELETE');
    if (generation !== epoch) return;
    draft = {}; stale = false; requestKey = null; paintForm(); feedback('삭제했습니다.');
    if (loading) await loading;
    await refresh(); window.RoomManager?.refresh(false);
  } catch (error) {
    if (generation !== epoch) return;
    if (error.status === 401) {reset(); R.toast(error.message); return;}
    if ([404, 409].includes(error.status)) {stale = true; paintForm(); await refresh();}
    feedback(error.message, true);
  } finally {if (generation === epoch) {busy = false; setBusy();}}
}
async function click(event) {
  const button = event.target.closest('[data-food-op]');
  if (!button || !root?.contains(button) || busy) return;
  const op = button.dataset.foodOp;
  if (op === 'refresh') {feedback(''); await refresh();}
  else if (op === 'new') {draft = {}; stale = false; requestKey = null; paintForm(); paintList(); feedback('');}
  else if (op === 'edit') {
    const value = items.find(x => x.id === button.dataset.id);
    if (value) {draft = structuredClone(value); stale = false; requestKey = null; paintForm(); paintList(); feedback('');}
  } else if (op === 'reload-item') {
    await refresh(); if (loadFailed) return;
    const value = items.find(x => x.id === draft.id);
    if (!value) {feedback('이 항목은 서버에서 삭제되었습니다. 새로 작성하려면 ‘새로 작성’을 누르세요.', true); return;}
    draft = structuredClone(value); stale = false; paintForm(); paintList(); feedback('서버의 최신 값으로 불러왔습니다. 확인 후 수정하세요.');
  } else if (op === 'delete') await remove();
}
function mount(element) {
  if (!element || root === element) return;
  collect(); if (root) root.removeEventListener('click', click);
  root = element; root.innerHTML = shell(); root.addEventListener('click', click);
  paintForm(); paintList(); refresh();
}
function reset() {
  epoch++; if (root) {root.removeEventListener('click', click); root.replaceChildren();}
  root = null; items = []; draft = {}; busy = false; stale = false; loading = null; requestKey = null; loadFailed = false;
  listClock = null;
}
window.RoomFood = {mount, refresh, reset};
})();
