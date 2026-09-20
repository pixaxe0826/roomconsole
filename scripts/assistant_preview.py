"""Build/open a standalone assistant demo. Data/model/actions are mock UI fixtures."""
from pathlib import Path
import json,shutil
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
def main():
    html=(ROOT/'previews/manager_preview.html').read_text()
    extra='''<script>(()=>{const t=setInterval(()=>{if(window.RoomManager?.getState()){clearInterval(t);RoomManager.navigate('llm')}},100);setTimeout(()=>clearInterval(t),10000)})();</script>'''
    html=html.replace('</body>',extra+'</body>')
    out=ROOT/'previews/assistant_manager_preview.html';out.write_text(html)
    with sync_playwright() as p:
        b=p.chromium.launch(executable_path=shutil.which('chromium'),args=['--no-sandbox'])
        pg=b.new_page(viewport={'width':1440,'height':1000});errors=[];pg.on('pageerror',lambda e:errors.append(str(e)))
        pg.set_content(html,wait_until='domcontentloaded');pg.wait_for_selector('[data-agent-state=awaiting_confirmation]')
        assert pg.locator('[data-llm=confirm-action]').is_visible()
        pg.locator('[data-llm=confirm-action]').click();pg.wait_for_selector('[data-agent-state=succeeded]')
        assert '예시 확인 완료' in pg.locator('.llm-output').inner_text()
        assert not errors
        b.close()
    result={'passed':3,'scope':'Standalone manager HTML with demo actions; no server/model; ready preview, confirm UI, no uncaught JS.'}
    (ROOT/'artifacts/test-results/assistant-preview.json').write_text(json.dumps(result,ensure_ascii=False))
    print('PASS standalone preview 3 checks')
if __name__=='__main__':main()
