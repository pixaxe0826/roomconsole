"""Preview-size regression against rebuilt HTML using Chromium set_content.
Not a physical iPad/Safari or live-network browser test.
"""
from __future__ import annotations
import argparse, json, os, shutil, time
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
REPORTS=ROOT/'artifacts'/'test-results';REPORTS.mkdir(parents=True,exist_ok=True)
SHOTS=ROOT/'artifacts'/'screenshots';SHOTS.mkdir(parents=True,exist_ok=True)
MEASURE="""() => {
 const box=document.querySelector('#previewBox'),frame=document.querySelector('#previewFrame');
 const br=box.getBoundingClientRect(),fr=frame.getBoundingClientRect();
 const cs=getComputedStyle(box),ps=getComputedStyle(box.parentElement);
 return {width:br.width,height:br.height,
  innerWidth:br.width-parseFloat(cs.borderLeftWidth)-parseFloat(cs.borderRightWidth),
  innerHeight:br.height-parseFloat(cs.borderTopWidth)-parseFloat(cs.borderBottomWidth),
  availableWidth:box.parentElement.clientWidth-parseFloat(ps.paddingLeft)-parseFloat(ps.paddingRight),
  frameWidth:fr.width,frameHeight:fr.height,inlineHeight:box.style.height,
  frameViewport:[frame.contentWindow.innerWidth,frame.contentWindow.innerHeight],
  transform:getComputedStyle(frame).transform};
}"""

def run(seconds=65):
 results=[];errors=[];samples=[]
 html=(ROOT/'previews/manager_preview.html').read_text(encoding='utf-8')
 def check(name,ok,detail=None):
  results.append({'name':name,'passed':bool(ok),'detail':detail})
  print(('PASS ' if ok else 'FAIL ')+name,flush=True)
  if not ok:raise AssertionError(f'{name}: {detail}')
 def geometry(page,label):
  m=page.evaluate(MEASURE)
  check(label+': fills parent width',abs(m['width']-m['availableWidth'])<=1,m)
  check(label+': fixed iframe viewport',m['frameViewport']==[1194,834])
  check(label+': frame fits box',0<m['frameWidth']<=m['innerWidth']+.15 and 0<m['frameHeight']<=m['innerHeight']+.15)
  check(label+': no JS-written height',m['inlineHeight']=='')
  return m
 with sync_playwright() as p:
  executable=os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome')
  browser=p.chromium.launch(executable_path=executable,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
  page=browser.new_page(viewport={'width':1440,'height':1000})
  page.on('pageerror',lambda e:errors.append(str(e)))
  page.evaluate("window.__previewErrors=[];window.addEventListener('error',e=>window.__previewErrors.push(e.message));")
  page.set_content(html,wait_until='domcontentloaded');page.wait_for_selector('#previewBox');page.wait_for_timeout(350)
  initial=geometry(page,'Initial dashboard')
  page.screenshot(path=str(SHOTS/'manager_overview_fixed.png'),full_page=True)
  start=time.monotonic()
  for i in range(seconds+1):
   samples.append({'seconds':round(time.monotonic()-start,3),**page.evaluate(MEASURE)})
   if i%10==0:print(f'IDLE {i}s: {samples[-1]["width"]} x {samples[-1]["height"]}',flush=True)
   if i<seconds:page.wait_for_timeout(1000)
  widths=[m['width'] for m in samples];heights=[m['height'] for m in samples]
  check(f'Stable dimensions for {seconds} seconds',max(widths)-min(widths)<=.1 and max(heights)-min(heights)<=.1,{'first':samples[0],'last':samples[-1]})
  page.screenshot(path=str(SHOTS/'manager_overview_after_idle.png'),full_page=True)
  for w,h in [(1920,1080),(1280,800),(1194,834),(1024,768),(980,800),(834,1194),(768,1024),(650,900),(390,844),(1440,1000)]:
   page.set_viewport_size({'width':w,'height':h});page.wait_for_timeout(200)
   a=geometry(page,f'{w}x{h}');page.wait_for_timeout(250);b=page.evaluate(MEASURE)
   check(f'{w}x{h}: no continued resize',abs(a['width']-b['width'])<=.1 and abs(a['height']-b['height'])<=.1)
  page.evaluate("document.querySelector('#previewBox').style.display='none'");page.wait_for_timeout(100)
  page.evaluate("document.querySelector('#previewBox').style.display=''");page.wait_for_timeout(200)
  a=geometry(page,'Hide / show');check('Hide / show restores width',abs(a['width']-initial['width'])<=.1)
  for _ in range(12):
   page.evaluate("RoomManager.navigate('tasks');RoomManager.navigate('overview')");page.wait_for_timeout(100)
  a=geometry(page,'Twelve tab return cycles');check('Tab return preserves width',abs(a['width']-initial['width'])<=.1)
  for _ in range(12):
   page.evaluate('RoomManager.refresh()');page.wait_for_timeout(100)
  a=geometry(page,'Twelve state refresh cycles');check('State refresh preserves width',abs(a['width']-initial['width'])<=.1)
  page.evaluate("RoomManager.navigate('tasks')")
  page.locator('#taskForm [name=title]').fill('크기 회귀 테스트')
  page.locator('#taskForm button[type=submit]').click();page.wait_for_timeout(150)
  page.evaluate("RoomManager.navigate('overview')");page.wait_for_timeout(250)
  frame=page.locator('#previewFrame').element_handle().content_frame()
  frame.wait_for_selector('#grid [data-widget-id=tasks]')
  check('Task mutation reaches preview','크기 회귀 테스트' in frame.locator('#grid').inner_text())
  page.locator('[data-op=remote][data-id=calendar]').click()
  frame.wait_for_selector('#focus:not(.hidden) [data-widget-id=calendar]');check('Remote expand works',True)
  page.locator('[data-op=remote][data-action=home]').click()
  frame.wait_for_selector('#display:not(.hidden)');check('Remote home works',True)
  geometry(page,'After remote commands')
  browser_errors=page.evaluate('window.__previewErrors')
  check('No JS / ResizeObserver errors',not errors and not browser_errors,{'page':errors,'window':browser_errors})
  fallback=browser.new_page(viewport={'width':1194,'height':834},device_scale_factor=2)
  fallback.on('pageerror',lambda e:errors.append(str(e)))
  fallback.evaluate('window.ResizeObserver=undefined')
  fallback.set_content(html,wait_until='domcontentloaded');fallback.wait_for_timeout(300)
  geometry(fallback,'No ResizeObserver / DPR 2')
  fallback.set_viewport_size({'width':834,'height':1194});fallback.wait_for_timeout(300)
  geometry(fallback,'Fallback portrait resize');check('No JS errors including fallback',not errors,errors)
  browser.close()
 output={'scope':'Chromium + actual rebuilt standalone HTML via Playwright set_content; not physical iPad/Safari or live browser network E2E','seconds':seconds,'checks':len(results),'passed':sum(r['passed'] for r in results),'results':results,'idle_samples':samples}
 (REPORTS/'preview-regression-results.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'passed':output['passed'],'checks':len(results),'first_width':samples[0]['width'],'last_width':samples[-1]['width'],'duration':samples[-1]['seconds'],'errors':errors},ensure_ascii=False))
 return output

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--seconds',type=int,default=65);args=parser.parse_args()
 if args.seconds<1:parser.error('--seconds must be positive')
 run(args.seconds)
