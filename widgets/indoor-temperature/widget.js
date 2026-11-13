// Device telemetry is read-only here. Device credentials never enter this widget.
const POLL_MS = 10000;
const REQUEST_MS = 8000;
const DEVICE_ID = /^[a-z][a-z0-9_-]{0,63}$/;
const STATES = new Set(['waiting', 'ok', 'stale', 'offline', 'sensor_error']);
const LABELS = {
  waiting: '첫 수신 대기', ok: '수신 중', stale: '수신 지연', offline: '연결 끊김',
  sensor_error: '센서 확인', connection: '서버 연결 끊김', permission: '권한 확인',
  unavailable: '설정 확인', invalid: '응답 확인', configuration: '기기 설정 확인',
  unregistered: '등록 필요', source_error: '조회 실패'
};
const PULL_LABELS = {waiting: '첫 조회 대기', ok: '조회 정상', stale: '조회 지연'};
const THERMOMETER = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 14.8V5a3 3 0 0 1 6 0v9.8a5 5 0 1 1-6 0Z"/><path d="M12 8v9"/><circle cx="12" cy="18" r="1.4" fill="currentColor" stroke="none"/></svg>';

function mono() { return typeof performance !== 'undefined' ? performance.now() : Date.now(); }
function finite(value) { return typeof value === 'number' && Number.isFinite(value); }
function dateValue(value) { return typeof value === 'string' ? Date.parse(value) : NaN; }
function deviceId(ctx) {
  const value = ctx.instance.config?.device_id;
  return value === undefined ? 'room-temp-01' : typeof value === 'string' ? value.trim() : '';
}

function validate(data, id) {
  const acquisition = data?.acquisition ?? 'push';
  const voltage = data?.voltage_v ?? null, adc = data?.adc_raw ?? null;
  const humidity = data?.humidity_percent ?? null, pressure = data?.pressure_hpa ?? null;
  const sourceKind = data?.source_kind ?? 'temperature';
  const sourceStatus = data?.source_status ?? null, lastAttempt = data?.last_attempt_at ?? null;
  if (!data || typeof data !== 'object' || data.device_id !== id ||
      typeof data.registered !== 'boolean' || !STATES.has(data.status) ||
      !['http_pull', 'push'].includes(acquisition) || !['environment', 'temperature'].includes(sourceKind) ||
      (humidity !== null && (!finite(humidity) || humidity < 0 || humidity > 100)) ||
      (pressure !== null && (!finite(pressure) || pressure <= 0)) ||
      ![true, false, null].includes(data.calibrated) ||
      (voltage !== null && !finite(voltage)) || (adc !== null && !finite(adc)) ||
      ![null, 'waiting', 'ok', 'error'].includes(sourceStatus) ||
      (lastAttempt !== null && !Number.isFinite(dateValue(lastAttempt))) ||
      ![true, false, null].includes(data.sensor_ok) ||
      (data.temperature_c !== null && !finite(data.temperature_c)) ||
      !Number.isFinite(dateValue(data.server_time)) ||
      (data.received_at !== null && !Number.isFinite(dateValue(data.received_at))) ||
      (data.age_seconds !== null && (!finite(data.age_seconds) || data.age_seconds < 0)) ||
      !Array.isArray(data.history) || data.history.length > 240) {
    throw Error('Invalid temperature response');
  }
  if ((data.sensor_ok === true) !== finite(data.temperature_c) ||
      (data.received_at === null) !== (data.age_seconds === null) ||
      (!data.registered && data.received_at !== null)) throw Error('Inconsistent temperature response');
  const stale = finite(data.stale_after_seconds) && data.stale_after_seconds > 0 ? data.stale_after_seconds : 30;
  const offline = finite(data.offline_after_seconds) && data.offline_after_seconds > stale ? data.offline_after_seconds : 120;
  // Retain only display fields, never a raw response containing future private fields.
  return {
    device_id: id, registered: data.registered, status: data.status,
    sensor_ok: data.sensor_ok, temperature_c: data.temperature_c,
    calibrated: data.calibrated, received_at: data.received_at,
    acquisition, source_kind: sourceKind, voltage_v: voltage, adc_raw: adc,
    humidity_percent: humidity, pressure_hpa: pressure,
    source_status: sourceStatus, last_attempt_at: lastAttempt,
    server_time: data.server_time, age_seconds: data.age_seconds,
    stale_after_seconds: stale, offline_after_seconds: offline,
    summary: finite(data.summary?.min_c) && finite(data.summary?.max_c) && data.summary.min_c <= data.summary.max_c ?
      {min_c: data.summary.min_c, max_c: data.summary.max_c} : null,
    history: data.history.filter(p => p && (finite(p.temperature_c) || p.temperature_c === null) && Number.isFinite(dateValue(p.at)))
      .map(p => ({at: p.at, temperature_c: p.temperature_c}))
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
  let v = owner.indoorTemperature;
  if (!v || v.id !== id || v.demo !== demo) {
    if (v) destroy(v);
    v = owner.indoorTemperature = {
      id, demo, data: null, error: null, roots: new Map(), seq: 0,
      pending: false, nextPoll: 0, receivedMono: 0, receivedWall: 0, timer: null
    };
  }
  if (!DEVICE_ID.test(id)) v.error = 'configuration';
  if (demo && DEVICE_ID.test(id)) {
    const source = ctx.state.indoor_temperature_demo?.[id];
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
  const pull = data?.acquisition === 'http_pull', sourceFailed = pull && data.source_status === 'error';
  const age = data?.age_seconds === null || !data ? null : data.age_seconds + elapsed(v);
  let status = v.error || 'waiting';
  if (!v.error && data) {
    status = !data.registered ? 'unregistered' : age === null ? sourceFailed ? 'source_error' : 'waiting' :
      data.status === 'offline' || age >= data.offline_after_seconds ? 'offline' :
      data.status === 'stale' || age >= data.stale_after_seconds ? 'stale' :
      sourceFailed ? 'source_error' :
      data.sensor_ok === true ? 'ok' : 'sensor_error';
  }
  const temperature = data && data.sensor_ok === true && finite(data.temperature_c) &&
    !['permission', 'unavailable', 'invalid', 'configuration'].includes(status) ? data.temperature_c : null;
  return {v, data, age, status, temperature, pull, sourceFailed};
}

function ageLabel(age, pull) {
  if (age === null) return pull ? '성공 조회 기록 없음' : '아직 수신 기록 없음';
  const duration = age < 60 ? `${Math.floor(age)}초` : age < 3600 ? `${Math.floor(age / 60)}분` :
    age < 86400 ? `${Math.floor(age / 3600)}시간` : `${Math.floor(age / 86400)}일`;
  return pull ? `성공 조회 ${duration} 전` : `${duration} 전 수신`;
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
  if (m.pull && ['waiting', 'unregistered', 'source_error', 'stale', 'offline', 'connection'].includes(m.status)) {
    if (m.sourceFailed && ['source_error', 'stale', 'offline'].includes(m.status)) {
      return m.temperature === null ? '직전 조회 실패 · 정상 응답을 기다립니다.' : '직전 조회 실패 · 마지막 정상 응답입니다.';
    }
    return {
      waiting: 'Odyssey의 첫 성공 조회를 기다립니다.', unregistered: 'Odyssey의 수집 설정을 확인하세요.',
      stale: '마지막 정상 응답 · 새 조회를 기다립니다.',
      offline: '마지막 정상 응답 · 센서 연결을 확인하세요.',
      connection: '마지막 정상 응답 · 서버 연결을 확인하세요.'
    }[m.status] || '';
  }
  return {
    waiting: '센서의 첫 전송을 기다립니다.', unregistered: '관리자에서 센서 등록을 확인하세요.',
    sensor_error: '측정 또는 센서 설정을 확인하세요.',
    stale: '마지막 기록입니다. 새 값을 기다립니다.',
    offline: '마지막 기록입니다. 센서 연결을 확인하세요.',
    connection: '마지막 기록입니다. 서버 연결을 확인하세요.',
    permission: '표시 기기 연결과 위젯 공유를 확인하세요.',
    unavailable: '장치 ID와 서버 업데이트를 확인하세요.',
    invalid: '서버에서 올바른 온도 응답을 받지 못했습니다.',
    configuration: '관리자에서 올바른 장치 ID를 지정하세요.'
  }[m.status] || '';
}

function chart(ctx, m) {
  const {esc} = ctx.util;
  const record = m.pull ? '조회' : '수신';
  const end = m.data ? dateValue(m.data.server_time) + elapsed(m.v) * 1000 : NaN;
  const start = end - 3600000;
  const points = (m.data?.history || []).map(p => ({x: dateValue(p.at), y: p.temperature_c}))
    .filter(p => p.x >= start && p.x <= end + 1000).sort((a, b) => a.x - b.x);
  const validPoints = points.filter(p => finite(p.y));
  if (!validPoints.length) return `<section class="it-history"><div class="it-history-head"><h3>최근 1시간</h3><span>${record} 기록 기준</span></div><div class="it-history-empty">${record}한 온도 기록이 쌓이면 여기에 표시됩니다.</div></section>`;
  const low = m.data.summary?.min_c ?? Math.min(...validPoints.map(p => p.y));
  const high = m.data.summary?.max_c ?? Math.max(...validPoints.map(p => p.y));
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
  return `<section class="it-history"><div class="it-history-head"><h3>최근 1시간</h3><span>${m.pull ? 'Odyssey 조회' : '수신'} · 1분 기록 · °C</span></div><svg class="it-chart" viewBox="0 0 480 148" role="img" aria-label="${esc(`최근 1시간 ${record} 기록. 기록 최저 ${low.toFixed(1)}도, 최고 ${high.toFixed(1)}도. 기록 공백은 선을 잇지 않습니다.`)}"><g class="it-chart-grid">${grid}</g><g class="it-chart-data">${lines}</g></svg><div class="it-chart-axis"><span>1시간 전</span><span>현재</span></div><div class="it-stats"><div><span>기록 최저</span><strong>${low.toFixed(1)}<small> °C</small></strong></div><div><span>기록 최고</span><strong>${high.toFixed(1)}<small> °C</small></strong></div><p>기록이 없는 구간은<br>선을 이어 표시하지 않습니다.</p></div></section>`;
}

function environmentValues(ctx, m) {
  const {esc} = ctx.util;
  if (m.data?.source_kind !== 'environment') return rawValues(ctx, m);
  const humidity = finite(m.data.humidity_percent) ? m.data.humidity_percent.toFixed(ctx.expanded ? 2 : 1) : '—';
  const pressure = finite(m.data.pressure_hpa) ? m.data.pressure_hpa.toFixed(ctx.expanded ? 2 : 1) : '—';
  if (!ctx.expanded) return `<div class="it-environment"><span>습도 <b data-temperature-humidity>${esc(humidity)}</b><small> %</small></span><span>기압 <b data-temperature-pressure>${esc(pressure)}</b><small> hPa</small></span></div>`;
  const attempt = timestamp(ctx, m.data.last_attempt_at) || '—';
  const source = {waiting: '대기', ok: '성공', error: '실패'}[m.data.source_status] || '확인 전';
  return `<table class="it-diagnostics" aria-label="습도·기압과 Odyssey 조회 상태"><tbody><tr><th scope="row">상대 습도</th><td><span data-temperature-humidity>${esc(humidity)}</span> %</td></tr><tr><th scope="row">기압</th><td><span data-temperature-pressure>${esc(pressure)}</span> hPa</td></tr><tr><th scope="row">마지막 조회 시도</th><td>${esc(attempt)}</td></tr><tr><th scope="row">조회 결과</th><td data-temperature-source-status>${esc(source)}</td></tr></tbody></table>`;
}

function rawValues(ctx, m) {
  if (!m.pull) return '';
  const {esc} = ctx.util;
  const voltage = finite(m.data?.voltage_v) ? m.data.voltage_v.toFixed(4) : '—';
  // ADC may be averaged by the source firmware. Preserve its fractional value.
  const adc = finite(m.data?.adc_raw) ? String(m.data.adc_raw) : '—';
  if (!ctx.expanded) return `<div class="it-raw"><span><b data-temperature-voltage>${esc(voltage)}</b> V</span><span>ADC <b data-temperature-adc>${esc(adc)}</b></span></div>`;
  const attempt = timestamp(ctx, m.data?.last_attempt_at) || '—';
  const source = {waiting: '대기', ok: '성공', error: '실패'}[m.data?.source_status] || '확인 전';
  return `<table class="it-diagnostics" aria-label="API 원시값과 Odyssey 조회 상태"><tbody><tr><th scope="row">전압</th><td><span data-temperature-voltage>${esc(voltage)}</span> V</td></tr><tr><th scope="row">ADC 원시값</th><td data-temperature-adc>${esc(adc)}</td></tr><tr><th scope="row">마지막 조회 시도</th><td>${esc(attempt)}</td></tr><tr><th scope="row">조회 결과</th><td data-temperature-source-status>${esc(source)}</td></tr></tbody></table>`;
}

export function render(ctx) {
  const m = model(ctx), {esc, icon} = ctx.util;
  const value = m.temperature === null ? '—' : m.temperature.toFixed(1);
  const readingLabel = m.temperature === null ? '온도 값 없음' : m.pull ?
    m.status === 'ok' ? 'API 제공 온도' : '마지막 정상 응답' : m.status === 'ok' ? '현재 온도' : '마지막 측정값';
  const received = timestamp(ctx, m.data?.received_at), note = message(m);
  const ageText = !m.data && m.v.error ? '조회 상태 확인 불가' : ageLabel(m.age, m.pull);
  const location = typeof ctx.instance.config?.location === 'string' ? ctx.instance.config.location : '내 방';
  const calibration = !m.pull && m.temperature !== null && m.data?.calibrated !== true ? '<span class="it-calibration">보정 미확인</span>' : '';
  const normalNote = m.pull ? 'Odyssey가 10초마다 조회' : 'Wi-Fi 센서 · 10초마다 확인';
  const expandedNote = m.pull ? m.data?.source_kind === 'environment' ? '온도·습도·기압은 API 제공값입니다. 기록 시각은 Odyssey의 성공 조회 시각이며 센서의 측정 시각은 아닙니다.' : 'API의 temperature_c를 그대로 표시합니다. 기록 시각은 Odyssey의 성공 조회 시각입니다.' :
    m.temperature !== null && m.data?.calibrated !== true ? '온도 정확도는 기준 온도계와 별도로 확인해 주세요.' : '';
  return `<div class="widget-content indoor-temperature-content ${ctx.expanded ? 'expanded' : ''} ${ctx.compact ? 'it-compact' : ''} ${m.pull ? 'it-pull' : ''}" data-temperature-status="${esc(m.status)}"><header class="it-head"><h2>${THERMOMETER}<span>${esc(ctx.instance.title || '실내 온도')}</span></h2><span class="it-status"><i aria-hidden="true"></i>${esc(m.pull && PULL_LABELS[m.status] || LABELS[m.status] || '확인 필요')}</span></header><div class="it-location">${icon('home', 12)}<span>${esc(location)}</span>${ctx.isDemo ? '<span class="it-demo">예시</span>' : ''}</div><div class="it-body"><div class="it-reading"><span class="it-reading-label">${esc(readingLabel)}</span><div class="it-number" aria-label="${esc(`${readingLabel} ${m.temperature === null ? '없음' : value + '도'}`)}"><strong data-temperature-value>${value}</strong><span class="it-unit">°C</span></div>${calibration}${environmentValues(ctx, m)}</div>${ctx.expanded ? chart(ctx, m) : ''}</div><footer class="it-footer"><div class="it-received"><span data-temperature-age>${esc(ageText)}</span>${received ? `<time datetime="${esc(m.data.received_at)}">${esc(received)}</time>` : ''}</div>${note ? `<p class="it-note">${esc(note)}</p>` : `<p class="it-note it-normal-note">${esc(normalNote)}</p>`}${ctx.expanded && expandedNote ? `<p class="it-origin-note">${esc(expandedNote)}</p>` : ''}</footer></div>`;
}

function paint(v) {
  for (const [root, ctx] of v.roots) {
    if (!root.isConnected) continue;
    const html = render(ctx);
    if (root._indoorTemperatureHTML !== html) { root.innerHTML = html; root._indoorTemperatureHTML = html; }
  }
}

async function request(v) {
  if (v.demo || v.pending || !v.roots.size || !DEVICE_ID.test(v.id)) return;
  const seq = ++v.seq, controller = new AbortController(), began = mono();
  v.abort = controller; v.pending = true;
  const timeout = setTimeout(() => controller.abort(), REQUEST_MS);
  try {
    const response = await fetch('/api/indoor-temperature/' + encodeURIComponent(v.id), {
      method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: controller.signal
    });
    if (!response.ok) { const error = Error('Temperature request failed'); error.status = response.status; throw error; }
    const text = await response.text();
    if (text.length > 65536) throw Error('Temperature response too large');
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
