// Read-only projection of the same source queried by Odyssey. No browser sensor credentials.
const POLL_MS = 10000;
const REQUEST_MS = 8000;
const DEVICE_ID = /^[a-z][a-z0-9_-]{0,63}$/;
const STATES = new Set(['waiting', 'ok', 'stale', 'offline', 'sensor_error']);
const LABELS = {
  waiting: '첫 조회 대기', ok: '조회 정상', stale: '조회 지연', offline: '연결 끊김',
  sensor_error: '센서 확인', connection: '서버 연결 끊김', permission: '권한 확인',
  unavailable: '설정 확인', invalid: '응답 확인', configuration: '기기 설정 확인',
  unregistered: '등록 필요', source_error: '조회 실패', unsupported: 'API 설정 확인'
};
const AIR_ICON = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8h13a3 3 0 1 0-3-3M2 12h17a3 3 0 1 1-3 3M4 16h5a3 3 0 1 1-3 3"/></svg>';
function mono() { return typeof performance !== 'undefined' ? performance.now() : Date.now(); }
function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
function dateValue(value) { return typeof value === 'string' ? Date.parse(value) : NaN; }
function raw(value) { return finite(value) ? Number.isInteger(value) ? value.toFixed(1) : String(value) : '—'; }
function deviceId(ctx) {
  const value = ctx.instance.config?.device_id;
  return value === undefined ? 'room-temp-01' : typeof value === 'string' ? value.trim() : '';
}
function validate(data, id) {
  if (!data || typeof data !== 'object' || data.device_id !== id ||
      typeof data.registered !== 'boolean' || !STATES.has(data.status) ||
      !['http_pull', 'push'].includes(data.acquisition) ||
      ![null, 'environment', 'temperature'].includes(data.source_kind) ||
      ![true, false, null].includes(data.sensor_ok) ||
      ![null, 'waiting', 'ok', 'error'].includes(data.source_status) ||
      !Number.isFinite(dateValue(data.server_time)) ||
      (data.last_attempt_at !== null && !Number.isFinite(dateValue(data.last_attempt_at))) ||
      (data.received_at !== null && !Number.isFinite(dateValue(data.received_at))) ||
      (data.age_seconds !== null && (!finite(data.age_seconds) || data.age_seconds < 0)) ||
      !Array.isArray(data.history) || data.history.length > 240) throw Error('Invalid air response');
  const fields = ['dust_adc_raw', 'dust_a0_voltage_v', 'dust_sensor_voltage_v'];
  if (fields.some(key => data[key] !== null && (!finite(data[key]) || data[key] < 0)) ||
      (data.received_at === null) !== (data.age_seconds === null) ||
      (!data.registered && data.received_at !== null)) throw Error('Inconsistent air response');
  const hasValues = fields.every(key => finite(data[key]));
  if (data.source_kind === 'environment' && (data.sensor_ok === true) !== hasValues) throw Error('Missing air data');
  const stale = finite(data.stale_after_seconds) && data.stale_after_seconds > 0 ? data.stale_after_seconds : 30;
  const offline = finite(data.offline_after_seconds) && data.offline_after_seconds > stale ? data.offline_after_seconds : 120;
  return {
    device_id: id, registered: data.registered, status: data.status, sensor_ok: data.sensor_ok,
    acquisition: data.acquisition, source_kind: data.source_kind,
    dust_adc_raw: data.dust_adc_raw, dust_a0_voltage_v: data.dust_a0_voltage_v,
    dust_sensor_voltage_v: data.dust_sensor_voltage_v,
    source_status: data.source_status, last_attempt_at: data.last_attempt_at,
    received_at: data.received_at, server_time: data.server_time, age_seconds: data.age_seconds,
    stale_after_seconds: stale, offline_after_seconds: offline,
    summary: finite(data.summary?.min_adc_raw) && finite(data.summary?.max_adc_raw) && data.summary.min_adc_raw <= data.summary.max_adc_raw ?
      {min_adc_raw: data.summary.min_adc_raw, max_adc_raw: data.summary.max_adc_raw} : null,
    history: data.history.filter(p => p && (finite(p.dust_adc_raw) || p.dust_adc_raw === null) && Number.isFinite(dateValue(p.at)))
      .map(p => ({at: p.at, dust_adc_raw: p.dust_adc_raw}))
  };
}
function destroy(v) {
  ++v.seq;
  v.abort?.abort();
  v.abort = null;
  v.pending = false;
  v.data = null;
  v.error = null;
  v.nextPoll = 0;
  v.receivedMono = 0;
  v.receivedWall = 0;
  clearInterval(v.timer);
  v.timer = null;
  if (v.visibility && typeof document !== 'undefined') document.removeEventListener('visibilitychange', v.visibility);
  v.visibility = null;
}

function session(ctx) {
  const owner = ctx.viewState || (ctx.viewState = {}), id = deviceId(ctx), demo = !!ctx.isDemo;
  let v = owner.indoorAirQuality;
  if (!v || v.id !== id || v.demo !== demo) {
    if (v) destroy(v);
    v = owner.indoorAirQuality = {
      id, demo, data: null, error: null, roots: new Map(), seq: 0,
      pending: false, nextPoll: 0, receivedMono: 0, receivedWall: 0, timer: null
    };
  }
  if (!DEVICE_ID.test(id)) v.error = 'configuration';
  if (demo && DEVICE_ID.test(id)) {
    const source = ctx.state.indoor_air_quality_demo?.[id];
    if (source) {
      try { v.data = validate(source, id); v.error = null; }
      catch { v.data = null; v.error = 'invalid'; }
    } else { v.data = null; v.error = null; }
  }
  return v;
}

function elapsed(v) {
  if (v.demo || !v.receivedMono) return 0;
  // Keep ageing if the OS suspended the page; a backwards wall-clock edit cannot freshen a value.
  return Math.max(0, (mono() - v.receivedMono) / 1000, (Date.now() - v.receivedWall) / 1000);
}

function model(ctx) {
  const v = session(ctx), data = v.data;
  const sourceFailed = data?.source_status === 'error';
  const age = data?.age_seconds === null || !data ? null : data.age_seconds + elapsed(v);
  let status = v.error || 'waiting';
  if (!v.error && data) {
    status = !data.registered ? 'unregistered' : data.source_kind !== 'environment' ? 'unsupported' :
      age === null ? sourceFailed ? 'source_error' : 'waiting' :
      data.status === 'offline' || age >= data.offline_after_seconds ? 'offline' :
      data.status === 'stale' || age >= data.stale_after_seconds ? 'stale' :
      sourceFailed ? 'source_error' : data.sensor_ok === true ? 'ok' : 'sensor_error';
  }
  const readable = data && data.sensor_ok === true && data.source_kind === 'environment' &&
    !['permission', 'unavailable', 'invalid', 'configuration', 'unsupported'].includes(status);
  const adc = readable && finite(data.dust_adc_raw) ? data.dust_adc_raw : null;
  return {v, data, age, status, adc, sourceFailed, readable};
}
function ageLabel(age) {
  if (age === null) return '성공 조회 기록 없음';
  const duration = age < 60 ? `${Math.floor(age)}초` : age < 3600 ? `${Math.floor(age / 60)}분` :
    age < 86400 ? `${Math.floor(age / 3600)}시간` : `${Math.floor(age / 86400)}일`;
  return `성공 조회 ${duration} 전`;
}
function timestamp(ctx, value) {
  if (!value) return '';
  try {
    return new Intl.DateTimeFormat('ko-KR', {
      timeZone: ctx.state.settings?.timezone || 'Asia/Seoul',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
    }).format(new Date(value));
  } catch { return ''; }
}

function message(m) {
  if (m.sourceFailed && ['source_error', 'stale', 'offline'].includes(m.status)) {
    return m.adc === null ? '직전 조회 실패 · 정상 응답을 기다립니다.' : '직전 조회 실패 · 마지막 정상 응답입니다.';
  }
  return {
    waiting: 'Odyssey의 첫 성공 조회를 기다립니다.', unregistered: 'Odyssey의 수집 설정을 확인하세요.',
    unsupported: '통합 센서 API(/api/sensors)를 설정하세요.', sensor_error: '먼지 센서 응답을 확인하세요.',
    stale: '마지막 정상 응답 · 새 조회를 기다립니다.', offline: '마지막 정상 응답 · 센서 연결을 확인하세요.',
    connection: '마지막 정상 응답 · 서버 연결을 확인하세요.', permission: '표시 기기 연결과 위젯 공유를 확인하세요.',
    unavailable: '장치 ID와 서버 업데이트를 확인하세요.', invalid: '서버에서 올바른 먼지 응답을 받지 못했습니다.',
    configuration: '관리자에서 올바른 장치 ID를 지정하세요.'
  }[m.status] || '';
}
function chart(ctx, m) {
  const {esc} = ctx.util;
  const record = '조회';
  const end = m.data ? dateValue(m.data.server_time) + elapsed(m.v) * 1000 : NaN;
  const start = end - 3600000;
  const points = (m.data?.history || []).map(p => ({x: dateValue(p.at), y: p.dust_adc_raw}))
    .filter(p => p.x >= start && p.x <= end + 1000).sort((a, b) => a.x - b.x);
  const validPoints = points.filter(p => finite(p.y));
  if (!validPoints.length) return `<section class="iq-history"><div class="iq-history-head"><h3>최근 1시간</h3><span>${record} 기록 기준</span></div><div class="iq-history-empty">${record}한 먼지 센서 기록이 쌓이면 여기에 표시됩니다.</div></section>`;
  const low = m.data.summary?.min_adc_raw ?? Math.min(...validPoints.map(p => p.y));
  const high = m.data.summary?.max_adc_raw ?? Math.max(...validPoints.map(p => p.y));
  const pad = Math.max(0.25, (high - low) * 0.15), bottom = low - pad, top = high + pad;
  const x = t => 38 + (t - start) / 3600000 * 424;
  const y = t => 12 + (top - t) / (top - bottom) * 112;
  const segments = []; let segment = [];
  for (const p of points) {
    if (p.y === null) { if (segment.length) segments.push(segment); segment = []; continue; }
    // A missing minute is a visible gap, never interpolated into a continuous measurement.
    if (segment.length && p.x - segment[segment.length - 1].x > 90000) { segments.push(segment); segment = []; }
    segment.push(p);
  }
  if (segment.length) segments.push(segment);
  const lines = segments.map(group => group.length === 1 ?
    `<circle cx="${x(group[0].x).toFixed(2)}" cy="${y(group[0].y).toFixed(2)}" r="3"/>` :
    `<polyline fill="none" points="${group.map(p => `${x(p.x).toFixed(2)},${y(p.y).toFixed(2)}`).join(' ')}"/>`).join('');
  const grid = [top, (top + bottom) / 2, bottom].map(value =>
    `<line x1="38" x2="462" y1="${y(value).toFixed(2)}" y2="${y(value).toFixed(2)}"/><text x="29" y="${(y(value) + 3).toFixed(2)}" text-anchor="end">${value.toFixed(1)}</text>`).join('');
  return `<section class="iq-history"><div class="iq-history-head"><h3>최근 1시간</h3><span>Odyssey 조회 · 1분 기록 · ADC</span></div><svg class="iq-chart" viewBox="0 0 480 148" role="img" aria-label="${esc(`최근 1시간 ${record} 기록. 기록 최저 ${raw(low)} ADC, 최고 ${raw(high)} ADC. 기록 공백은 선을 잇지 않습니다.`)}"><g class="iq-chart-grid">${grid}</g><g class="iq-chart-data">${lines}</g></svg><div class="iq-chart-axis"><span>1시간 전</span><span>현재</span></div><div class="iq-stats"><div><span>기록 최저</span><strong>${raw(low)}<small> ADC</small></strong></div><div><span>기록 최고</span><strong>${raw(high)}<small> ADC</small></strong></div><p>기록이 없는 구간은<br>선을 이어 표시하지 않습니다.</p></div></section>`;
}

function diagnostics(ctx, m) {
  const {esc} = ctx.util;
  const a0 = m.readable && finite(m.data?.dust_a0_voltage_v) ? m.data.dust_a0_voltage_v.toFixed(4) : '—';
  const output = m.readable && finite(m.data?.dust_sensor_voltage_v) ? m.data.dust_sensor_voltage_v.toFixed(4) : '—';
  if (!ctx.expanded) return `<div class="iq-raw"><span>출력 <b data-air-output>${esc(output)}</b> V</span><span>A0 <b data-air-a0>${esc(a0)}</b> V</span></div>`;
  const attempt = timestamp(ctx, m.data?.last_attempt_at) || '—';
  const source = {waiting: '대기', ok: '성공', error: '실패'}[m.data?.source_status] || '확인 전';
  return `<table class="iq-diagnostics" aria-label="먼지 센서 원시값과 Odyssey 조회 상태"><tbody><tr><th scope="row">센서 출력 전압</th><td><span data-air-output>${esc(output)}</span> V</td></tr><tr><th scope="row">보드 A0 전압</th><td><span data-air-a0>${esc(a0)}</span> V</td></tr><tr><th scope="row">마지막 조회 시도</th><td>${esc(attempt)}</td></tr><tr><th scope="row">조회 결과</th><td data-air-source-status>${esc(source)}</td></tr></tbody></table>`;
}
export function render(ctx) {
  const m = model(ctx), {esc, icon} = ctx.util;
  const value = raw(m.adc);
  const readingLabel = m.adc === null ? '먼지 센서 · 값 없음' : m.status === 'ok' ? '먼지 센서 · 원시값' : '마지막 정상 응답';
  const received = timestamp(ctx, m.data?.received_at), note = message(m);
  const ageText = !m.data && m.v.error ? '조회 상태 확인 불가' : ageLabel(m.age);
  const location = typeof ctx.instance.config?.location === 'string' ? ctx.instance.config.location : '내 방';
  return `<div class="widget-content indoor-air-quality-content ${ctx.expanded ? 'expanded' : ''} ${ctx.compact ? 'iq-compact' : ''}" data-air-status="${esc(m.status)}"><header class="iq-head"><h2>${AIR_ICON}<span>${esc(ctx.instance.title || '실내 공기 질')}</span></h2><span class="iq-status"><i aria-hidden="true"></i>${esc(LABELS[m.status] || '확인 필요')}</span></header><div class="iq-location">${icon('home', 12)}<span>${esc(location)}</span>${ctx.isDemo ? '<span class="iq-demo">예시</span>' : ''}</div><div class="iq-body"><div class="iq-reading"><span class="iq-reading-label">${esc(readingLabel)}</span><div class="iq-number ${value.length > 7 ? 'iq-long' : ''}" aria-label="${esc(`먼지 센서 ADC 원시값 ${m.adc === null ? '없음' : value}`)}"><strong data-air-value>${esc(value)}</strong><span class="iq-unit">ADC</span></div><span class="iq-calibration">농도 미보정</span>${diagnostics(ctx, m)}</div>${ctx.expanded ? chart(ctx, m) : ''}</div><footer class="iq-footer"><div class="iq-received"><span data-air-age>${esc(ageText)}</span>${received ? `<time datetime="${esc(m.data.received_at)}">${esc(received)}</time>` : ''}</div>${note ? `<p class="iq-note">${esc(note)}</p>` : '<p class="iq-note iq-normal-note">Odyssey가 10초마다 조회</p>'}${ctx.expanded ? '<p class="iq-origin-note">먼지 센서의 ADC·전압 원시값입니다. 미세먼지 농도(µg/m³)·PM2.5·AQI·좋음/나쁨 등급으로 환산하지 않습니다. 기록 시각은 Odyssey의 성공 조회 시각입니다.</p>' : ''}</footer></div>`;
}
function paint(v) {
  for (const [root, ctx] of v.roots) {
    if (!root.isConnected) continue;
    const html = render(ctx);
    if (root._indoorAirQualityHTML !== html) { root.innerHTML = html; root._indoorAirQualityHTML = html; }
  }
}

async function request(v) {
  if (v.demo || v.pending || !v.roots.size || !DEVICE_ID.test(v.id)) return;
  const seq = ++v.seq, controller = new AbortController(), began = mono();
  v.abort = controller; v.pending = true;
  const timeout = setTimeout(() => controller.abort(), REQUEST_MS);
  try {
    const response = await fetch('/api/indoor-air-quality/' + encodeURIComponent(v.id), {
      method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: controller.signal
    });
    if (!response.ok) { const error = Error('Air request failed'); error.status = response.status; throw error; }
    const text = await response.text();
    if (text.length > 65536) throw Error('Air response too large');
    let data;
    try { data = validate(JSON.parse(text), v.id); } catch { const error = Error('Invalid temperature data'); error.invalid = true; throw error; }
    if (v.seq !== seq) return;
    v.data = data; v.error = null; v.receivedMono = mono(); v.receivedWall = Date.now();
  } catch (error) {
    if (v.seq !== seq) return;
    v.error = [401, 403].includes(error.status) ? 'permission' : error.status === 404 ? 'unavailable' : error.invalid ? 'invalid' : 'connection';
    if (['permission', 'unavailable', 'invalid'].includes(v.error)) v.data = null;
  } finally {
    clearTimeout(timeout);
    if (v.seq === seq) {
      v.pending = false; v.abort = null; v.nextPoll = Math.max(began + POLL_MS, mono() + 250);
      paint(v);
    }
  }
}

export function bind(root, ctx) {
  const v = session(ctx);
  v.roots.set(root, ctx);
  // A static synthetic preview must never contact a real server or age into a fabricated state.
  if (!v.demo && DEVICE_ID.test(v.id) && !v.timer) {
    const tick = () => {
      paint(v);
      if (!document.hidden && mono() >= v.nextPoll) request(v);
    };
    v.timer = setInterval(tick, 1000);
    v.visibility = () => { if (!document.hidden) { v.nextPoll = 0; tick(); } };
    document.addEventListener('visibilitychange', v.visibility);
    if (!document.hidden && mono() >= v.nextPoll) request(v);
  }
  return () => {
    v.roots.delete(root);
    queueMicrotask(() => {
      // Normal Room redraw binds replacement grid/focus roots before this microtask.
      if (!v.roots.size) destroy(v);
    });
  };
}
