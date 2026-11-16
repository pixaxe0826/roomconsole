// Read-only projection. Food changes belong to the manager/confirmed Agent path.
const CHUNK = 50;

function dayNumber(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return null;
  const time = Date.parse(value + 'T00:00:00Z');
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value
    ? time / 86400000 : null;
}

function due(item, today) {
  const expiry = dayNumber(item.expiry_date), now = dayNumber(today);
  if (expiry === null || now === null) return {kind: 'unknown', label: '날짜 확인 필요'};
  const days = expiry - now;
  if (days < 0) return {kind: 'expired', label: `기한 ${-days}일 지남`};
  if (days === 0) return {kind: 'today', label: '오늘까지'};
  return {kind: days <= 3 ? 'soon' : 'later', label: `${days}일 남음`};
}

function model(ctx) {
  const view = ctx.viewState || (ctx.viewState = {});
  view.foodLimit = Math.max(CHUNK, Number(view.foodLimit) || CHUNK);
  const data = ctx.state.food;
  const items = data?.enabled === true && Array.isArray(data.items)
    ? [...data.items].sort((a, b) => String(a.expiry_date).localeCompare(String(b.expiry_date))
      || String(a.name).localeCompare(String(b.name), 'ko') || String(a.id).localeCompare(String(b.id)))
    : [];
  // The client updates state.today at the server-zone midnight without a new GET.
  const today = ctx.state.today || data?.today;
  return {view, data, items, today, attention: items.filter(x => ['expired', 'today', 'soon'].includes(due(x, today).kind)).length};
}

export function render(ctx) {
  const {esc, icon} = ctx.util, m = model(ctx), shown = Math.min(m.items.length, m.view.foodLimit);
  const rows = m.items.slice(0, shown).map(item => {
    const status = due(item, m.today);
    return `<tr class="food-expiry-${status.kind}" data-food-id="${esc(item.id)}"><th scope="row">${esc(item.name)}</th><td class="food-expiry-quantity">${esc(item.quantity)}<span class="food-expiry-unit">개</span></td><td><time datetime="${esc(item.expiry_date)}">${esc(item.expiry_date)}</time><span class="food-expiry-badge">${esc(status.label)}</span></td></tr>`;
  }).join('');
  const empty = !m.data ? '서버 데이터를 불러오지 못했습니다.'
    : !m.data.enabled ? '배치를 저장하면 등록한 식품이 표시됩니다.'
      : '등록한 식품이 없습니다. 관리자 → 소비기한 알리미에서 추가하세요.';
  return `<div class="widget-content food-expiry-content ${ctx.expanded ? 'expanded' : ''} ${ctx.compact ? 'compact' : ''}">
    <div class="widget-head"><h2>${icon('leaf')} ${esc(ctx.instance.title || '소비기한 알리미')}</h2><button type="button" class="icon-button food-expiry-expand" data-stop data-food-expand aria-label="소비기한 알리미 크게 보기">${icon('expand', 17)}</button></div>
    <p class="food-expiry-summary">${m.items.length}종 등록 · ${m.attention ? `<strong>날짜 확인 ${m.attention}종</strong>` : '소비기한순'}${ctx.connected === false ? ' · 오프라인, 마지막 목록' : ''}</p>
    <div class="todo-list food-expiry-scroll" data-stop tabindex="0" role="region" aria-label="식품·개수·소비기한 목록">
      <table class="food-expiry-table"><caption>소비기한순 식품 목록</caption><colgroup><col class="food-expiry-name-col"><col class="food-expiry-count-col"><col class="food-expiry-date-col"></colgroup><thead><tr><th scope="col">식품</th><th scope="col">개수</th><th scope="col">소비기한</th></tr></thead><tbody>${rows || `<tr><td colspan="3" class="food-expiry-empty">${esc(empty)}</td></tr>`}</tbody></table>
      ${shown < m.items.length ? `<button type="button" class="btn small food-expiry-more" data-stop data-food-more>다음 ${Math.min(CHUNK, m.items.length - shown)}종 더 보기 · ${shown}/${m.items.length}</button>` : ''}
    </div>
    <p class="food-expiry-footnote">오늘·3일 이내·지난 날짜 표시 · 입력한 소비기한 기준</p>
  </div>`;
}

export function bind(root, ctx) {
  const click = event => {
    const button = event.target.closest('[data-food-more],[data-food-expand]');
    if (!button || !root.contains(button)) return;
    event.stopPropagation();
    if (button.hasAttribute('data-food-expand')) ctx.expand();
    else {model(ctx).view.foodLimit += CHUNK; ctx.redraw();}
  };
  root.addEventListener('click', click);
  return () => root.removeEventListener('click', click);
}
