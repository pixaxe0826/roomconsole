"""Build an offline two-widget preview from the actual production widget modules.

All values, timestamps and states are synthetic; no API or sensor is contacted.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build() -> Path:
    now = datetime(2026, 10, 25, 15, 15, 0, tzinfo=timezone.utc)
    iso = lambda value: value.isoformat().replace('+00:00', 'Z')
    common = {
        'device_id': 'room-temp-01', 'registered': True, 'status': 'ok',
        'sensor_ok': True, 'acquisition': 'http_pull', 'source_kind': 'environment',
        'source_status': 'ok', 'source_error': None,
        'last_attempt_at': iso(now - timedelta(seconds=2)),
        'received_at': iso(now - timedelta(seconds=2)), 'server_time': iso(now),
        'age_seconds': 2, 'stale_after_seconds': 30, 'offline_after_seconds': 120,
        'history_bucket_seconds': 60,
    }
    temperatures = [{'at': iso(now-timedelta(minutes=59-i)),
                     'temperature_c': round(26.0+i*.013+math.sin(i/6)*.12, 2),
                     'humidity_percent': round(40.5-i*.005+math.cos(i/7)*.1, 2),
                     'pressure_hpa': round(1009.0+i*.0015, 2)}
                    for i in range(60) if not 26 <= i <= 31]
    dust = [{'at': iso(now-timedelta(minutes=59-i)),
             'dust_adc_raw': round(26.0+math.sin(i/6)*1.1+max(0, 1-abs(22-i)/6)*7.7, 1),
             'dust_a0_voltage_v': .0697, 'dust_sensor_voltage_v': .2922}
            for i in range(60) if not 26 <= i <= 31]
    temperatures[-1].update(temperature_c=26.78, humidity_percent=40.13, pressure_hpa=1009.09)
    dust[-1].update(dust_adc_raw=26.0, dust_a0_voltage_v=.0697, dust_sensor_voltage_v=.2922)
    temp = {**common, 'temperature_c': 26.78, 'humidity_percent': 40.13,
            'pressure_hpa': 1009.09, 'voltage_v': None, 'adc_raw': None,
            'calibrated': None, 'history': temperatures,
            'summary': {'min_c': min(p['temperature_c'] for p in temperatures),
                        'max_c': max(p['temperature_c'] for p in temperatures)}}
    air = {**common, 'dust_adc_raw': 26.0, 'dust_a0_voltage_v': .0697,
           'dust_sensor_voltage_v': .2922, 'history': dust,
           'summary': {'min_adc_raw': min(p['dust_adc_raw'] for p in dust),
                       'max_adc_raw': max(p['dust_adc_raw'] for p in dust)}}
    states = {
        'temperature': temp, 'air': air,
        'temp-stale': {**temp, 'status': 'stale', 'source_status': 'error', 'source_error': 'timeout',
                       'age_seconds': 45, 'received_at': iso(now-timedelta(seconds=45))},
        'air-offline': {**air, 'status': 'offline', 'source_status': 'error', 'source_error': 'connection',
                        'age_seconds': 135, 'received_at': iso(now-timedelta(seconds=135))},
        'temp-waiting': {**temp, 'status': 'waiting', 'sensor_ok': None, 'source_status': 'waiting',
                         'temperature_c': None, 'humidity_percent': None, 'pressure_hpa': None,
                         'last_attempt_at': None, 'received_at': None, 'age_seconds': None, 'history': []},
        'air-error': {**air, 'source_status': 'error', 'source_error': 'timeout',
                      'age_seconds': 15, 'received_at': iso(now-timedelta(seconds=15))},
    }
    css = '\n'.join((ROOT/name).read_text('utf-8') for name in (
        'web/base.css', 'web/client.css', 'widgets/indoor-temperature/style.css',
        'widgets/indoor-air-quality/style.css'))
    modules = []
    for widget, name in (('indoor-temperature', 'TemperatureWidget'), ('indoor-air-quality', 'AirWidget')):
        source = (ROOT/f'widgets/{widget}/widget.js').read_text('utf-8').replace('export function', 'function')
        modules.append(f'window.{name} = (() => {{\n{source}\nreturn {{render,bind}};\n}})();')
    safe = lambda value: json.dumps(value, ensure_ascii=False).replace('<', '\\u003c')
    html = '''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Room Hub · 실내 환경 위젯 미리보기</title>
<style>__CSS__
body{height:auto;min-height:100vh;padding:40px 30px 50px;overflow:auto}
.environment-preview{max-width:1140px;margin:0 auto}
.preview-header{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;padding-bottom:28px;border-bottom:1px solid var(--line)}
.preview-header h1{font-size:33px;font-weight:600;margin-top:12px;letter-spacing:-.05em}
.preview-header p{font-size:13px;line-height:1.8;color:var(--muted);margin-top:12px;max-width:650px}
.preview-tag{padding:7px 11px;font-size:10px;color:var(--accent);background:var(--accent-soft);border-radius:30px;white-space:nowrap}
.preview-section{margin-top:27px}.preview-section>h2{font-size:13px;font-weight:600;letter-spacing:-.02em;margin-bottom:15px;color:var(--muted)}
.preview-cards{display:grid;grid-template-columns:1fr 1fr .85fr;gap:20px;align-items:start}
.preview-card-frame{min-width:0}.preview-card-frame>p{display:flex;justify-content:space-between;gap:5px;font-size:11px;color:var(--muted);padding:10px 3px 0}
.preview-card-frame>.widget{height:245px;max-width:100%;border-radius:22px}
.preview-cards .it-number strong,.preview-cards .iq-number strong{font-size:67px}
.preview-cards .it-head h2,.preview-cards .iq-head h2{font-size:14px}
.preview-cards .it-status,.preview-cards .iq-status{font-size:10px}
.preview-cards .it-location,.preview-cards .iq-location{font-size:11px}
.preview-cards .it-environment{font-size:11px;gap:13px}.preview-cards .iq-raw{font-size:10px;gap:12px}
.preview-cards .it-received,.preview-cards .iq-received{font-size:10px}
.preview-cards .it-note,.preview-cards .iq-note{font-size:9px}
.preview-info{padding:20px 4px 12px 14px;font-size:12px;line-height:1.8;color:var(--muted)}
.preview-info h3{font-size:16px;color:var(--ink);font-weight:550;margin-bottom:10px}
.preview-info p+p{margin-top:12px}.preview-info b{color:var(--ink);font-weight:550}
.preview-tabs{display:flex;gap:7px;margin-bottom:14px;flex-wrap:wrap}
.preview-tabs button{border:1px solid var(--line);background:var(--surface);color:var(--muted);border-radius:9px;padding:8px 14px;font-size:12px;cursor:pointer}
.preview-tabs button[aria-selected=true]{background:var(--accent-soft);color:var(--accent);border-color:var(--accent)}
.preview-tabs button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.preview-expanded{height:680px}.preview-expanded[hidden]{display:none}
.preview-secondary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.preview-secondary .widget{height:230px}
.preview-bottom{font-size:11px;color:var(--muted);line-height:1.8;margin-top:22px;padding-top:18px;border-top:1px solid var(--line)}
@media(max-width:980px){.preview-cards{grid-template-columns:1fr 1fr}.preview-info{grid-column:1/-1;padding:6px 4px;display:grid;grid-template-columns:1fr 1fr;gap:16px}.preview-info h3{grid-column:1/-1;margin:0}.preview-info p+p{margin:0}.preview-secondary{grid-template-columns:repeat(2,minmax(0,1fr))}.preview-expanded{height:670px}}
@media(max-width:760px){body{padding:25px 18px}.preview-header{flex-direction:column;gap:12px}.preview-header h1{font-size:27px}.preview-cards{gap:14px}.preview-expanded{height:790px}.preview-cards .it-head h2,.preview-cards .iq-head h2{font-size:11px}.preview-cards .it-status,.preview-cards .iq-status{font-size:8px}.preview-cards .it-number strong,.preview-cards .iq-number strong{font-size:48px}.preview-cards .it-environment,.preview-cards .iq-raw{font-size:9px}}
@media(max-width:480px){.preview-cards,.preview-secondary{grid-template-columns:1fr}.preview-card-frame>.widget{max-width:330px;margin:auto}.preview-card-frame>p{max-width:330px;margin:auto}.preview-info{display:block}.preview-info h3{margin:10px 0}.preview-info p+p{margin-top:12px}.preview-expanded{height:800px}}
</style></head><body><main class="environment-preview">
<header class="preview-header"><div><div class="eyebrow">ROOM HUB / INDOOR ENVIRONMENT</div><h1>내 방의 환경을, 한눈에.</h1><p>온도·습도·기압과 먼지 센서의 변화를 두 개의 위젯으로.<br>조회 상태와 마지막 정상 응답 시각을 함께 보여줍니다.</p></div><span class="preview-tag">디자인 미리보기 · 모두 예시 데이터</span></header>
<section class="preview-section"><h2>기본 카드 · 각각 2×2</h2><div class="preview-cards">
<div class="preview-card-frame"><section class="widget" id="temperature"></section><p><span>실내 온도</span><span>온도 · 습도 · 기압</span></p></div>
<div class="preview-card-frame"><section class="widget" id="air"></section><p><span>실내 공기 질</span><span>먼지 센서 원시값</span></p></div>
<aside class="preview-info"><h3>하나의 센서, 두 개의 위젯.</h3><p>Odyssey가 <b>10초마다</b> 통합 API를 조회합니다. 두 위젯을 동시에 배치해도 센서 수집은 한 번입니다.</p><p>공기 질 카드는 <b>ADC와 전압</b>을 표시합니다. 보정 전에는 농도나 좋음·나쁨 등급을 표시하지 않습니다.</p></aside>
</div></section>
<section class="preview-section"><h2>확대 화면 · 최근 1시간의 변화와 조회 내역</h2>
<div class="preview-tabs" role="tablist" aria-label="확대할 위젯"><button id="tab-temperature" role="tab" aria-selected="true" aria-controls="expanded-temperature" tabindex="0">온도 · 습도 · 기압</button><button id="tab-air" role="tab" aria-selected="false" aria-controls="expanded-air" tabindex="-1">공기 질 · 원시값</button></div>
<section class="widget preview-expanded" id="expanded-temperature" role="tabpanel" aria-labelledby="tab-temperature"></section>
<section class="widget preview-expanded" id="expanded-air" role="tabpanel" aria-labelledby="tab-air" hidden></section>
</section>
<section class="preview-section"><h2>조회가 늦거나 연결이 끊기면, 마지막 정상값임을 명확하게.</h2><div class="preview-secondary">
<div class="preview-card-frame"><section class="widget" id="temp-waiting"></section><p><span>첫 성공 조회 전</span><span>값 없음</span></p></div>
<div class="preview-card-frame"><section class="widget" id="air-error"></section><p><span>직전 조회 실패</span><span>마지막 값</span></p></div>
<div class="preview-card-frame"><section class="widget" id="temp-stale"></section><p><span>30초 이상 갱신 없음</span><span>조회 지연</span></p></div>
<div class="preview-card-frame"><section class="widget" id="air-offline"></section><p><span>120초 이상 갱신 없음</span><span>연결 끊김</span></p></div>
</div></section>
<p class="preview-bottom">이 화면은 실제 widget.js·style.css로 만든 예시이며 센서나 서버에 연결하지 않습니다. 모든 수치와 시각은 합성 데이터입니다.<br>조회 시각은 Odyssey가 정상 응답을 받은 시각입니다. API에 센서의 측정 시각이 없으므로 실제 측정 시각을 뜻하지 않습니다. 먼지 원시값을 PM2.5·PM10·AQI로 환산하지 않습니다.</p>
</main><script>__SCRIPT__</script></body></html>'''
    boot = f'''
const fixtures = {safe(states)};
for (const [id, data] of Object.entries({{...fixtures, 'expanded-temperature':fixtures.temperature, 'expanded-air':fixtures.air}})) {{
  const isAir=id==='air'||id.includes('air-')||id==='expanded-air';
  const root=document.getElementById(id), module=isAir?AirWidget:TemperatureWidget;
  const ctx={{instance:{{id:'preview-'+id,type:isAir?'indoor-air-quality':'indoor-temperature',title:isAir?'실내 공기 질':'실내 온도',config:{{device_id:'room-temp-01',location:'내 방'}}}},
    state:{{settings:{{timezone:'Asia/Seoul'}},[isAir?'indoor_air_quality_demo':'indoor_temperature_demo']:{{'room-temp-01':data}}}},
    viewState:{{}},util:Room,isDemo:true,connected:true,expanded:id.startsWith('expanded-'),compact:id.includes('-')&&!id.startsWith('expanded-'),redraw:()=>{{}},now:new Date(data.server_time)}};
  root.innerHTML=module.render(ctx);module.bind(root,ctx);
}}
function chooseTab(name, focus=false){{
  for(const kind of ['temperature','air']){{
    const active=kind===name, tab=document.getElementById('tab-'+kind);
    tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;
    document.getElementById('expanded-'+kind).hidden=!active;
    if(active&&focus)tab.focus();
  }}
}}
for(const name of ['temperature','air']){{
  const tab=document.getElementById('tab-'+name);
  tab.addEventListener('click',()=>chooseTab(name));
  tab.addEventListener('keydown',event=>{{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){{event.preventDefault();chooseTab(event.key==='Home'?'temperature':event.key==='End'?'air':name==='air'?'temperature':'air',true);}}}});
}}
'''
    shared=(ROOT/'web/shared.js').read_text('utf-8')
    html=html.replace('__CSS__',css).replace('__SCRIPT__',(shared+'\n'+'\n'.join(modules)+boot).replace('</script','<\\/script'))
    path=ROOT/'previews/indoor_temperature_preview.html'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(html,encoding='utf-8',newline='\n')
    return path


if __name__=='__main__':
    print(build())
