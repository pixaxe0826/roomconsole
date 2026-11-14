"""Chromium policy-form regression with synthetic admin API; no real OpenAI traffic."""

import json
import os
from pathlib import Path
import shutil

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]

MOCK_API = r"""() => {
  window.RoomManager = {
    getTab: () => 'llm',
    getState: () => ({settings:{timezone:'Asia/Seoul'}}),
    navigate() {},
    async refresh() {},
  };
  window.__policy = {
    agent_enabled:false, cloud_enabled:false, audio_cloud_enabled:false,
    allow_shared_notes_in_cloud:false, monthly_cap_usd:100,
  };
  window.__writes = [];
  window.__failure = null;
  window.fetch = async (url, options={}) => {
    const method = options.method || 'GET';
    const reply = (body, status=200) => new Response(JSON.stringify(body), {
      status, headers:{'Content-Type':'application/json'},
    });
    if (url === '/api/llm/config') return reply({
      runtime_profile:'odyssey_openai',
      config:{enabled:false,base_url:'http://127.0.0.1:8179',model:'legacy-only'},
    });
    if (url.startsWith('/api/llm/requests?')) return reply({items:[],total:0});
    if (url.startsWith('/api/agent/runs?')) return reply({items:[]});
    if (url === '/api/agent/status') return reply({
      runtime_profile:'odyssey_openai', ...window.__policy,
      budget:{monthly_cap_usd:window.__policy.monthly_cap_usd,committed_usd:0,state:'NORMAL'},
      cloud_status:'agent_disabled',adapters:[],tiers:{tier1_stt:'synthetic STT'},
    });
    if (url === '/api/agent/budget/policy' && method === 'PUT') {
      window.__writes.push({url,method,headers:options.headers,credentials:options.credentials,
                            body:JSON.parse(options.body)});
      if (window.__failure) return reply(window.__failure.body,window.__failure.status);
      Object.assign(window.__policy,JSON.parse(options.body));
      return reply({policy:window.__policy,budget:{monthly_cap_usd:window.__policy.monthly_cap_usd}});
    }
    throw Error('Unexpected synthetic request: '+method+' '+url);
  };
}"""


def run():
    checks = []

    def check(name, condition):
        assert condition, name
        checks.append(name)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome'),
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content('<!doctype html><html lang="ko"><meta charset="utf-8"><body class="manager-body"><main id="llmRoot"></main></body></html>')
        for stylesheet in ('web/base.css', 'web/manager.css', 'web/manager-llm.css'):
            page.add_style_tag(content=(ROOT / stylesheet).read_text(encoding='utf-8'))
        page.add_script_tag(content=(ROOT / 'web/shared.js').read_text(encoding='utf-8'))
        page.evaluate(MOCK_API)
        page.add_script_tag(content=(ROOT / 'web/manager-llm.js').read_text(encoding='utf-8'))
        page.evaluate("RoomLLM.mount(document.getElementById('llmRoot'))")
        page.wait_for_function("document.querySelector('#agentMonthlyCap')?.value === '100'")

        def dimensions():
            return page.locator('#agentMonthlyCap').evaluate('''el => ({
              width:el.getBoundingClientRect().width,
              height:el.getBoundingClientRect().height,
              labelDirection:getComputedStyle(el.closest('label')).flexDirection,
              overflow:document.documentElement.scrollWidth > innerWidth,
            })''')

        for width in (1440, 390):
            page.set_viewport_size({'width': width, 'height': 900})
            size = dimensions()
            check(f'USD amount is legible at {width}px',
                  size['width'] >= 140 and size['height'] >= 40)
            check(f'Policy labels align and do not overflow at {width}px',
                  size['labelDirection'] == 'row' and not size['overflow'])

        page.set_viewport_size({'width': 1440, 'height': 900})
        page.locator('#agentEnabledToggle').check()
        page.locator('#agentCloudToggle').check()
        page.locator('#agentAudioCloudToggle').check()
        page.locator('#agentMonthlyCap').fill('42.50')
        page.locator('[data-llm="agent-save-policy"]').click()
        page.wait_for_function("document.querySelector('#agentPolicyFeedback').textContent.includes('저장했습니다')")
        sent = page.evaluate('window.__writes')
        check('Manager policy uses one authenticated same-origin PUT',
              len(sent) == 1 and sent[0]['method'] == 'PUT' and
              sent[0]['credentials'] == 'same-origin' and
              sent[0]['headers'].get('X-Room-Request') == '1')
        check('Saved settings preserve three opt-ins and the entered USD amount',
              all(sent[0]['body'][key] is True for key in
                  ('agent_enabled', 'cloud_enabled', 'audio_cloud_enabled')) and
              sent[0]['body']['monthly_cap_usd'] == 42.5)

        page.evaluate("window.__failure={status:422,body:{detail:[{loc:['query','request'],msg:'Field required'}]}}")
        page.locator('[data-llm="agent-save-policy"]').click()
        page.wait_for_function("document.querySelector('#agentPolicyFeedback').textContent.includes('request: Field required')")
        check('422 validation response names the failing field, not generic permissions',
              '권한' not in page.locator('#agentPolicyFeedback').inner_text())
        page.evaluate("window.__failure={status:401,body:{}}")
        page.locator('[data-llm="agent-save-policy"]').click()
        page.wait_for_function("document.querySelector('#agentPolicyFeedback').textContent.includes('관리자 로그인이 필요합니다')")
        check('401 has an actionable sign-in message', True)
        check('No agent command, transcription, or provider call during policy UI tests',
              all(x['url'] == '/api/agent/budget/policy' for x in page.evaluate('window.__writes')))
        check('No uncaught JavaScript errors', not errors)

        shots = ROOT / 'artifacts/screenshots'
        shots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots / 'agent-policy-fixed-desktop.png'))
        browser.close()

    reports = ROOT / 'artifacts/test-results'
    reports.mkdir(parents=True, exist_ok=True)
    (reports / 'agent-policy-browser.json').write_text(
        json.dumps({'checks': checks, 'passed': len(checks),
                    'scope': 'Chromium actual CSS/JS + mocked admin API; no real network/cloud/DB.'},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'PASS {len(checks)} policy browser checks (synthetic, no paid calls)')


if __name__ == '__main__':
    run()
