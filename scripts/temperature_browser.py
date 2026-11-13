"""Chromium tests of both production environment widgets with synthetic HTTP responses.

Actual ES modules and Room CSS run under an intercepted HTTPS origin.
No physical LAN, Arduino, Odyssey, iPad/Safari or sensor accuracy is tested.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from build_temperature_preview import build

ROOT=Path(__file__).resolve().parents[1]
ORIGIN='https://environment.test'
PAGE='''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/web/base.css"><link rel="stylesheet" href="/web/client.css"><link rel="stylesheet" href="/widgets/indoor-temperature/style.css"><link rel="stylesheet" href="/widgets/indoor-air-quality/style.css">
<script src="/web/shared.js" defer></script><script src="/harness.js" type="module"></script></head><body class="client-body"><main class="display-shell"><header class="display-header">합성 실내 환경 위젯 검사</header><div class="dashboard-grid" id="grid"></div><footer class="display-footer">실기기 측정 아님</footer></main></body></html>'''
HARNESS='''
import * as temperature from '/widgets/indoor-temperature/widget.js';
import * as air from '/widgets/indoor-air-quality/widget.js';
let viewState={}, cleanups=[], kind='temperature';
let options={device_id:'room-temp-01',location:'내 방'};
function cleanup(){cleanups.forEach(f=>f());cleanups=[];document.querySelector('#grid').replaceChildren();document.querySelector('#focus')?.remove();}
function mount(next={},expanded=false,both=false){
  options={...options,...next};cleanup();
  const grid=document.querySelector('#grid');
  const make=(selected,isExpanded,other=false)=>{
    const module=selected==='air'?air:temperature;
    const root=document.createElement('section');root.className='widget';root.dataset.widgetId=selected+'-test';
    const rect=grid.getBoundingClientRect();
    const width=(rect.width-7*16)/8*2+16,height=(rect.height-5*16)/6*2+16;
    if(!isExpanded){root.style.gridColumn=(other?'3':'1')+' / span 2';root.style.gridRow='1 / span 2';grid.append(root);}
    else{const focus=document.createElement('div');focus.id='focus';focus.className='focus-view';focus.innerHTML='<header class="focus-header">확대 화면</header><div id="focusContent"></div>';document.body.append(focus);focus.querySelector('#focusContent').append(root);}
    const owner=other?(viewState.other||(viewState.other={})):viewState;
    const ctx={state:{settings:{timezone:'Asia/Seoul'}},instance:{id:selected+'-test',type:selected==='air'?'indoor-air-quality':'indoor-temperature',title:options.title||(selected==='air'?'실내 공기 질':'실내 온도'),config:{device_id:options.device_id,location:options.location}},viewState:owner,util:Room,isDemo:false,connected:true,expanded:isExpanded,compact:!isExpanded&&(width<245||height<155),now:new Date(),redraw:()=>{}};
    root.innerHTML=module.render(ctx);cleanups.push(module.bind(root,ctx));return root;
  };
  make(kind,false);if(expanded)make(kind,true);if(both)make(kind==='air'?'temperature':'air',false,true);
}
window.environmentHarness={mount,unmount:cleanup,select:async next=>{cleanup();await new Promise(queueMicrotask);kind=next;viewState={};options={device_id:'room-temp-01',location:'내 방'};mount();},reset:async()=>{cleanup();await new Promise(queueMicrotask);viewState={};options={device_id:'room-temp-01',location:'내 방'};mount();}};
mount();
'''


def main():
    output=ROOT/'artifacts/test-results';shots=ROOT/'artifacts/screenshots'
    output.mkdir(parents=True,exist_ok=True);shots.mkdir(parents=True,exist_ok=True)
    report={'scope':__doc__,'checks':[],'page_errors':[],'external_requests':[]}
    def check(name,value):
        report['checks'].append({'name':name,'passed':bool(value)})
        print(('PASS ' if value else 'FAIL ')+name,flush=True)
        assert value,name
    now=datetime(2026,10,25,15,15,tzinfo=timezone.utc)
    iso=lambda t:t.isoformat().replace('+00:00','Z')
    common={'device_id':'room-temp-01','registered':True,'status':'ok','sensor_ok':True,
            'acquisition':'http_pull','source_kind':'environment','source_status':'ok','source_error':None,
            'last_attempt_at':iso(now-timedelta(seconds=2)), 'received_at':iso(now-timedelta(seconds=2)),
            'server_time':iso(now),'age_seconds':2,'stale_after_seconds':30,'offline_after_seconds':120,'history_bucket_seconds':60}
    temperature={**common,'temperature_c':26.78,'humidity_percent':40.13,'pressure_hpa':1009.09,
                 'voltage_v':None,'adc_raw':None,'calibrated':None,
                 'history':[{'at':iso(now-timedelta(minutes=2)),'temperature_c':26.5},{'at':iso(now-timedelta(minutes=1)),'temperature_c':None},{'at':iso(now),'temperature_c':26.78}],
                 'summary':{'min_c':26.4,'max_c':27.1}}
    air={**common,'dust_adc_raw':26.0,'dust_a0_voltage_v':.0697,'dust_sensor_voltage_v':.2922,
         'history':[{'at':iso(now-timedelta(minutes=2)),'dust_adc_raw':24.5},{'at':iso(now-timedelta(minutes=1)),'dust_adc_raw':None},{'at':iso(now),'dust_adc_raw':26.0}],
         'summary':{'min_adc_raw':23.75,'max_adc_raw':27.125}}
    responses={'temperature':{'status':200,'body':temperature},'air':{'status':200,'body':air}}
    requests=[]
    paths={'temperature':'/api/indoor-temperature/','air':'/api/indoor-air-quality/'}
    def route_request(route):
        request=route.request
        if not request.url.startswith(ORIGIN+'/'):
            report['external_requests'].append(request.url);route.abort();return
        path=request.url[len(ORIGIN):].split('?',1)[0]
        kind=next((kind for kind,prefix in paths.items() if path.startswith(prefix)),None)
        if kind:
            requests.append({'kind':kind,'url':path,'method':request.method,'headers':request.headers})
            response=responses[kind]
            route.fulfill(status=response['status'],content_type='application/json',body=json.dumps(response['body']))
        elif path=='/environment-test':
            route.fulfill(status=200,content_type='text/html; charset=utf-8',body=PAGE,headers={'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self'; base-uri 'none'"})
        elif path=='/harness.js':route.fulfill(status=200,content_type='application/javascript; charset=utf-8',body=HARNESS)
        elif path in {'/web/base.css','/web/client.css','/web/shared.js','/widgets/indoor-temperature/style.css','/widgets/indoor-temperature/widget.js','/widgets/indoor-air-quality/style.css','/widgets/indoor-air-quality/widget.js'}:
            route.fulfill(status=200,content_type='text/css; charset=utf-8' if path.endswith('.css') else 'application/javascript; charset=utf-8',body=(ROOT/path.lstrip('/')).read_text('utf-8'))
        elif path=='/preview':route.fulfill(status=200,content_type='text/html; charset=utf-8',body=build().read_text('utf-8'))
        else:route.fulfill(status=404,body='not found')
    def fits(page,selector,prefix):
        return page.locator(selector).evaluate('''(e,p)=>{
          const box=e.getBoundingClientRect(),body=e.querySelector('.'+p+'-body').getBoundingClientRect(),footer=e.querySelector('.'+p+'-footer').getBoundingClientRect(),location=e.querySelector('.'+p+'-location').getBoundingClientRect();
          return e.scrollWidth<=e.clientWidth+1&&e.scrollHeight<=e.clientHeight+1&&
            [...e.querySelectorAll('.'+p+'-head,.'+p+'-reading,.'+p+'-history,.'+p+'-footer')].every(child=>{const b=child.getBoundingClientRect();return b.left>=box.left-1&&b.right<=box.right+1&&b.top>=box.top-1&&b.bottom<=box.bottom+1;})&&body.top>=location.bottom-1&&body.bottom<=footer.top+1&&
            [...e.querySelectorAll('.'+p+'-reading,.'+p+'-history')].every(child=>{const b=child.getBoundingClientRect();return b.top>=location.bottom-1&&b.bottom<=footer.top+1;});
        }''',prefix)
    try:
        with sync_playwright() as playwright:
            opts={'headless':True,'args':['--no-sandbox']}
            executable=os.getenv('CHROMIUM_PATH') or os.getenv('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or shutil.which('chromium')
            if executable:opts['executable_path']=executable
            browser=playwright.chromium.launch(**opts)
            page=browser.new_page(viewport={'width':1194,'height':834},has_touch=True)
            page.on('pageerror',lambda e:report['page_errors'].append(str(e)));page.route('**/*',route_request);page.clock.install(time=now)
            page.goto(ORIGIN+'/environment-test')
            for kind,good,prefix,initial in [('temperature',temperature,'it','26.8'),('air',air,'iq','26.0')]:
                if kind=='air':page.evaluate("environmentHarness.select('air')")
                page.set_viewport_size({'width':1194,'height':834});page.evaluate('environmentHarness.mount()')
                data='temperature' if kind=='temperature' else 'air'
                widget='indoor-temperature' if kind=='temperature' else 'indoor-air-quality'
                value=page.locator(f'#grid [data-{data}-value]');status=page.locator(f'#grid [data-{data}-status]')
                expect(value).to_have_text(initial)
                count=len(requests)
                check(f'{kind}: actual ES module uses a same-origin GET under strict CSP',requests[-1]['kind']==kind and requests[-1]['method']=='GET')
                check(f'{kind}: successful query time is explicit','성공 조회' in page.locator(f'#grid [data-{data}-age]').inner_text())
                check(f'{kind}: 1194×834 default 2×2 fits',fits(page,f'#grid .{widget}-content',prefix))
                if kind=='temperature':
                    check('Temperature card shows humidity and pressure',page.locator('#grid [data-temperature-humidity]').inner_text()=='40.1' and page.locator('#grid [data-temperature-pressure]').inner_text()=='1009.1')
                    check('Temperature card does not relabel dust voltage as temperature voltage',page.locator('#grid [data-temperature-voltage]').count()==0)
                else:
                    check('Air card preserves fractional ADC representation and both four-decimal voltages',page.locator('#grid [data-air-output]').inner_text()=='0.2922' and page.locator('#grid [data-air-a0]').inner_text()=='0.0697')
                    check('Air card explicitly marks concentration uncalibrated',page.locator('#grid .iq-calibration').inner_text()=='농도 미보정' and '원시값' in page.locator('#grid .iq-reading-label').inner_text())
                    check('Air card does not invent AQI or a quality grade',not any(s in page.locator('#grid').inner_text() for s in ['좋음','나쁨','PM2.5','µg/m³','AQI']))
                page.screenshot(path=str(shots/f'environment-{kind}-1194.png'))
                page.evaluate('environmentHarness.mount({},true)');expect(page.locator(f'#focus [data-{data}-value]')).to_have_text(initial)
                check(f'{kind}: grid and focus share one poll session',len(requests)==count)
                check(f'{kind}: null history bucket breaks the line',page.locator(f'#focus .{prefix}-chart-data polyline').count()==0 and page.locator(f'#focus .{prefix}-chart-data circle').count()==2)
                expected_extrema=('26.4','27.1') if kind=='temperature' else ('23.75','27.125')
                check(f'{kind}: server raw extrema preserved',all(v in page.locator(f'#focus .{prefix}-stats').inner_text() for v in expected_extrema))
                check(f'{kind}: expanded query status shown',page.locator(f'#focus [data-{data}-source-status]').inner_text()=='성공')
                check(f'{kind}: expanded timestamp describes Odyssey success, not sensor sampling','Odyssey의 성공 조회 시각' in page.locator(f'#focus .{prefix}-origin-note').inner_text())
                if kind=='temperature':check('Expanded humidity and pressure keep two supplied decimals',page.locator('#focus [data-temperature-humidity]').inner_text()=='40.13' and page.locator('#focus [data-temperature-pressure]').inner_text()=='1009.09')
                check(f'{kind}: landscape expanded view fits',fits(page,f'#focus .{widget}-content',prefix))
                page.screenshot(path=str(shots/f'environment-{kind}-expanded-1194.png'))
                page.clock.fast_forward(11000);expect(value).to_have_text(initial)
                check(f'{kind}: grid and focus still make only one poll per interval',len(requests)==count+1)
                page.evaluate('environmentHarness.mount()')
                for width,height in ((834,1194),(1024,768)):
                    page.set_viewport_size({'width':width,'height':height});page.evaluate('environmentHarness.mount()')
                    check(f'{kind}: {width}×{height} default 2×2 fits',fits(page,f'#grid .{widget}-content',prefix))
                    page.screenshot(path=str(shots/f'environment-{kind}-{width}.png'))
                page.set_viewport_size({'width':834,'height':1194});page.evaluate('environmentHarness.mount({},true)')
                check(f'{kind}: portrait expanded view fits',fits(page,f'#focus .{widget}-content',prefix))
                page.screenshot(path=str(shots/f'environment-{kind}-expanded-834.png'));page.evaluate('environmentHarness.mount()')
                def refresh(body,code=200):
                    responses[kind]={'status':code,'body':body};page.clock.fast_forward(11000)
                zeros={'temperature_c':0,'humidity_percent':0} if kind=='temperature' else {'dust_adc_raw':0,'dust_a0_voltage_v':0,'dust_sensor_voltage_v':0}
                refresh({**good,**zeros});expect(value).to_have_text('0.0')
                check(f'{kind}: valid zero stays numeric',value.inner_text()=='0.0')
                if kind=='temperature':
                    refresh({**good,'humidity_percent':None,'pressure_hpa':None});expect(page.locator('#grid [data-temperature-humidity]')).to_have_text('—')
                    check('Missing humidity and pressure do not become zero',value.inner_text()=='26.8' and page.locator('#grid [data-temperature-pressure]').inner_text()=='—')
                    bad={'humidity_percent':'40.13'}
                else:
                    refresh({**good,'dust_adc_raw':26.125});expect(value).to_have_text('26.125')
                    check('Averaged ADC retains fractional digits',value.inner_text()=='26.125')
                    refresh({**good,'dust_a0_voltage_v':.1234,'dust_sensor_voltage_v':.9876});expect(value).to_have_text(initial)
                    check('Dust ADC is never derived from voltage in the widget',page.locator('#grid [data-air-output]').inner_text()=='0.9876')
                    bad={'dust_a0_voltage_v':'0.0697'}
                refresh({**good,**bad});expect(status).to_have_attribute(f'data-{data}-status','invalid')
                check(f'{kind}: numeric strings are rejected',value.inner_text()=='—')
                refresh({**good,'age_seconds':29});expect(status).to_have_attribute(f'data-{data}-status','ok');page.clock.fast_forward(2000)
                expect(status).to_have_attribute(f'data-{data}-status','stale')
                check(f'{kind}: local age crosses 30-second stale threshold','마지막 정상 응답' in page.locator(f'#grid .{prefix}-reading-label').inner_text())
                refresh({**good,'status':'stale','age_seconds':119});expect(status).to_have_attribute(f'data-{data}-status','stale');page.clock.fast_forward(2000)
                expect(status).to_have_attribute(f'data-{data}-status','offline');check(f'{kind}: local age crosses 120-second offline threshold','마지막' in page.locator(f'#grid .{prefix}-note').inner_text())
                refresh({**good,'status':'stale','age_seconds':0});expect(status).to_have_attribute(f'data-{data}-status','stale')
                check(f'{kind}: server stale cannot be promoted by local zero age',status.get_attribute(f'data-{data}-status')=='stale')
                refresh({**good,'source_status':'error','source_error':'timeout','age_seconds':12});expect(status).to_have_attribute(f'data-{data}-status','source_error')
                check(f'{kind}: immediate source failure clearly retains last-good value',value.inner_text()==initial and '직전 조회 실패' in page.locator(f'#grid .{prefix}-note').inner_text())
                for state,age in [('stale',45),('offline',135)]:
                    refresh({**good,'status':state,'source_status':'error','source_error':'connection','age_seconds':age});expect(status).to_have_attribute(f'data-{data}-status',state)
                    check(f'{kind}: source failure preserves {state} classification','마지막 정상 응답' in page.locator(f'#grid .{prefix}-reading-label').inner_text())
                empty_fields={'temperature_c':None,'humidity_percent':None,'pressure_hpa':None} if kind=='temperature' else {'dust_adc_raw':None,'dust_a0_voltage_v':None,'dust_sensor_voltage_v':None}
                waiting={**good,**empty_fields,'status':'waiting','sensor_ok':None,'source_status':'waiting','last_attempt_at':None,'received_at':None,'age_seconds':None,'history':[]}
                refresh(waiting);expect(status).to_have_attribute(f'data-{data}-status','waiting')
                check(f'{kind}: first query has no fabricated zero',value.inner_text()=='—')
                refresh({**waiting,'source_status':'error','source_error':'timeout','last_attempt_at':iso(now)});expect(status).to_have_attribute(f'data-{data}-status','source_error')
                check(f'{kind}: failed first query has no fabricated last-good time','성공 조회 기록 없음' in page.locator(f'#grid [data-{data}-age]').inner_text())
                refresh({**waiting,'registered':False,'source_kind':None,'acquisition':'push','source_status':None});expect(status).to_have_attribute(f'data-{data}-status','unregistered')
                check(f'{kind}: unregistered device is distinct from malformed data',value.inner_text()=='—')
                if kind=='air':
                    refresh({**waiting,'source_kind':'temperature'});expect(status).to_have_attribute('data-air-status','unsupported')
                    check('Legacy temperature source cannot masquerade as dust data','/api/sensors' in page.locator('#grid .iq-note').inner_text() and value.inner_text()=='—')
                    refresh({**good,'dust_adc_raw':None});expect(status).to_have_attribute('data-air-status','invalid')
                    check('Incomplete current dust fields do not become a valid reading',value.inner_text()=='—')
                else:
                    refresh({**good,'source_kind':'temperature','acquisition':'push','calibrated':False,'source_status':None});expect(page.locator('#grid .it-calibration')).to_have_text('보정 미확인')
                    check('Legacy push keeps its original calibration caution',page.locator('#grid .it-calibration').count()==1)
                    refresh({**good,'source_kind':'temperature','voltage_v':.268,'adc_raw':100.1});expect(page.locator('#grid [data-temperature-adc]')).to_have_text('100.1')
                    check('Legacy temperature API raw fields remain distinct and compatible',page.locator('#grid [data-temperature-voltage]').inner_text()=='0.2680')
                refresh(good);expect(value).to_have_text(initial)
                refresh({'detail':'synthetic failure'},503);expect(status).to_have_attribute(f'data-{data}-status','connection')
                check(f'{kind}: server failure keeps a clearly old value',value.inner_text()==initial and '마지막' in page.locator(f'#grid .{prefix}-reading-label').inner_text())
                for code,state in [(401,'permission'),(403,'permission'),(404,'unavailable')]:
                    refresh(good);expect(value).to_have_text(initial);refresh({'detail':'denied'},code);expect(status).to_have_attribute(f'data-{data}-status',state)
                    check(f'{kind}: {code} removes previously readable values',value.inner_text()=='—')
                refresh(good);expect(value).to_have_text(initial)
                page.evaluate("environmentHarness.mount({title:'<img src=x onerror=window.ENV_BAD=1>',location:'<script>window.ENV_BAD=1</script>'})")
                check(f'{kind}: configuration text is escaped',page.locator('#grid img').count()==0 and not page.evaluate('Boolean(window.ENV_BAD)'))
                page.evaluate('''async()=>{window.originalFetch=window.fetch;window.held=[];window.aborts=0;window.fetch=(url,options={})=>new Promise(resolve=>{window.held.push(resolve);options.signal?.addEventListener('abort',()=>{window.aborts++;},{once:true});});await environmentHarness.reset();}''')
                page.clock.fast_forward(4000);check(f'{kind}: requests cannot overlap',page.evaluate('held.length')==1)
                page.evaluate('async()=>{environmentHarness.unmount();await Promise.resolve();}');check(f'{kind}: unmount aborts in-flight request',page.evaluate('aborts')==1)
                page.evaluate('()=>{window.fetch=originalFetch;environmentHarness.mount();}');expect(value).to_have_text(initial)
                late={**good,**({'temperature_c':61.1} if kind=='temperature' else {'dust_adc_raw':61.125})}
                page.evaluate("data=>held[0](new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}}))",late);page.clock.fast_forward(1000)
                check(f'{kind}: late aborted response cannot overwrite remount',value.inner_text()==initial)
                count=len(requests);page.evaluate("environmentHarness.mount({device_id:'INVALID-UPPERCASE'})");expect(status).to_have_attribute(f'data-{data}-status','configuration');page.clock.fast_forward(11000)
                check(f'{kind}: invalid ID never falls back or fetches',len(requests)==count and value.inner_text()=='—')
                page.evaluate('environmentHarness.unmount()');count=len(requests);page.clock.fast_forward(30000);check(f'{kind}: unmount stops polling',len(requests)==count)
            responses['temperature']={'status':200,'body':temperature};responses['air']={'status':200,'body':air}
            page.evaluate("environmentHarness.select('temperature')");expect(page.locator('#grid [data-temperature-value]')).to_have_text('26.8');page.evaluate('environmentHarness.mount({},false,true)');expect(page.locator('#grid [data-air-value]')).to_have_text('26.0')
            check('Both widgets coexist without state or CSS collisions',page.locator('#grid [data-temperature-value]').inner_text()=='26.8' and page.locator('#grid [data-air-value]').inner_text()=='26.0')
            page.set_viewport_size({'width':1194,'height':834});page.evaluate('environmentHarness.mount({},false,true)');page.screenshot(path=str(shots/'environment-both-widgets.png'))
            page.evaluate('environmentHarness.unmount()')
            preview=browser.new_page(viewport={'width':1194,'height':834},has_touch=True);preview.on('pageerror',lambda e:report['page_errors'].append(str(e)));preview.route('**/*',route_request)
            before=len(requests);preview.goto(ORIGIN+'/preview');expect(preview.locator('#temperature [data-temperature-value]')).to_have_text('26.8');expect(preview.locator('#air [data-air-value]')).to_have_text('26.0')
            check('Standalone preview explicitly labels all data synthetic','모두 예시 데이터' in preview.locator('.preview-tag').inner_text())
            check('Standalone preview makes no API calls',len(requests)==before)
            check('Standalone preview default and state cards fit',all(fits(preview,f'#{id} .{widget}-content',prefix) for id,widget,prefix in [('temperature','indoor-temperature','it'),('air','indoor-air-quality','iq'),('temp-waiting','indoor-temperature','it'),('temp-stale','indoor-temperature','it'),('air-error','indoor-air-quality','iq'),('air-offline','indoor-air-quality','iq')]))
            check('Standalone expanded temperature view fits',fits(preview,'#expanded-temperature .indoor-temperature-content','it'))
            preview.screenshot(path=str(shots/'environment-preview-temperature.png'),full_page=True)
            preview.locator('#tab-air').click();expect(preview.locator('#expanded-air')).to_be_visible()
            check('Standalone expanded air view fits',fits(preview,'#expanded-air .indoor-air-quality-content','iq'))
            check('Air expanded preview explains calibration limits','PM2.5' in preview.locator('#expanded-air .iq-origin-note').inner_text() and '환산하지 않습니다' in preview.locator('#expanded-air .iq-origin-note').inner_text())
            preview.screenshot(path=str(shots/'environment-preview-air.png'),full_page=True)
            preview.locator('#tab-air').press('ArrowLeft');expect(preview.locator('#tab-temperature')).to_have_attribute('aria-selected','true')
            check('Preview tabs support keyboard navigation',preview.locator('#expanded-temperature').is_visible())
            check('No sensor/admin token in browser requests',all('authorization' not in row['headers'] for row in requests))
            check('No uncaught JavaScript exceptions',not report['page_errors'])
            check('No external network requests',not report['external_requests'])
            check('Browser never calls NodeMCU directly',all(row['url'].startswith(paths[row['kind']]) for row in requests) and not report['external_requests'])
            browser.close()
    finally:
        report['passed']=sum(row['passed'] for row in report['checks'])
        (output/'temperature-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('TOTAL',report['passed'])


if __name__=='__main__':main()
