/* Production food widget in a Node VM. Synthetic data only; no browser or network. */
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const ROOT = path.resolve(__dirname, '..'), context = {Intl, Date, Number, String, Math};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/shared.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'widgets/food-expiry/widget.js'), 'utf8').replace(/export function/g, 'function') + '\nwindow.FoodWidget={render,bind};', context);
const checks = [], check = (name, passed) => {assert.ok(passed, name); checks.push(name);};
const items = [
  {id:'later', name:'합성 식품', quantity:2, expiry_date:'2026-11-20'},
  {id:'expired', name:'<img src=x onerror=BAD()>', quantity:3, expiry_date:'2026-11-15'},
  {id:'today', name:'오늘 식품', quantity:1, expiry_date:'2026-11-16'},
  {id:'soon', name:'임박 식품', quantity:4, expiry_date:'2026-11-19'},
];
const ctx = {state:{today:'2026-11-16', food:{enabled:true, today:'2026-11-15', items}},
  instance:{title:'소비기한 알리미'}, util:context.Room, viewState:{}, connected:true};
let html = context.FoodWidget.render(ctx);
check('Exactly three ordered table headers', html.includes('<th scope="col">식품</th><th scope="col">개수</th><th scope="col">소비기한</th>'));
check('Sorted by calendar expiry without mutating source array', html.indexOf('data-food-id="expired"') < html.indexOf('data-food-id="today"') && items[0].id === 'later');
check('Names are escaped inert text', html.includes('&lt;img') && !html.includes('<img'));
check('No paired-display edit controls', !/<(?:input|textarea|select)\b|contenteditable|data-food-op=/.test(html));
check('Expired, today and 3-day imminent are labelled in text', html.includes('기한 1일 지남') && html.includes('오늘까지') && html.includes('3일 남음'));
check('Local server-zone today wins over stale projection date', html.includes('food-expiry-today" data-food-id="today"'));
check('Missing date is not guessed', context.FoodWidget.render({...ctx,state:{food:{enabled:true,items}}}).includes('날짜 확인 필요'));
ctx.state.today = '2026-11-17';
check('Badges roll across midnight without changing stored rows', context.FoodWidget.render(ctx).includes('food-expiry-expired" data-food-id="today"') && items[2].expiry_date === '2026-11-16');
check('Disabled projection never renders supplied item content', !context.FoodWidget.render({...ctx,state:{today:'2026-11-16',food:{enabled:false,items}}}).includes('합성 식품'));
check('Missing projection is explicitly unavailable', context.FoodWidget.render({...ctx,state:{today:'2026-11-16'}}).includes('서버 데이터를 불러오지 못했습니다.'));
check('Disconnected state is explicit', context.FoodWidget.render({...ctx,connected:false}).includes('오프라인, 마지막 목록'));
const many = Array.from({length:173}, (_,i)=>({id:'item-'+String(i).padStart(3,'0'), name:'합성 '+i, quantity:1, expiry_date:'2027-01-01'}));
ctx.state.food.items = many; ctx.viewState = {};
html = context.FoodWidget.render(ctx);
check('Initial DOM is bounded but has a reachable load-more control', (html.match(/data-food-id=/g)||[]).length === 50 && html.includes('data-food-more'));
let listener, cleaned = false, redraws = 0, expands = 0;
ctx.redraw = () => {redraws++;}; ctx.expand = () => {expands++;};
const root = {addEventListener:(name,fn)=>{listener=fn;},removeEventListener:(name,fn)=>{cleaned=fn===listener;},contains:()=>true};
const cleanup = context.FoodWidget.bind(root,ctx);
const click = name => listener({target:{closest:()=>({hasAttribute:key=>key===name})},stopPropagation(){}});
for (let i=0;i<3;i++) click('data-food-more');
html = context.FoodWidget.render(ctx);
check('All 173 items including beyond row 150 remain accessible', (html.match(/data-food-id=/g)||[]).length === 173 && html.includes('data-food-id="item-172"') && !html.includes('data-food-more'));
check('Pagination changes only local view state', redraws === 3 && many.length === 173);
click('data-food-expand'); cleanup();
check('Expansion uses only the display navigation hook and listener is cleaned', expands === 1 && cleaned);
async function editorRegression() {
  const decode = text => text.replace(/&(?:amp|lt|gt|quot|#39);/g, s=>({'&amp;':'&','&lt;':'<','&gt;':'>','&quot;':'"','&#39;':"'"}[s]));
  function matches(element, selector) {
    const tag = selector.match(/^[a-z]+/i); if (tag && element.tagName !== tag[0].toUpperCase()) return false;
    const cl = selector.match(/\.([\w-]+)/); if (cl && !element.classes.has(cl[1])) return false;
    for (const attr of selector.matchAll(/\[([\w-]+)(?:=["']?([^\]"']+)["']?)?\]/g)) {
      if (!(attr[1] in element.attrs) || attr[2] !== undefined && element.attrs[attr[1]] !== attr[2]) return false;
    }
    return true;
  }
  class Element {
    constructor(tag='div', parent=null, attrs={}) {
      this.tagName=tag.toUpperCase();this.parent=parent;this.attrs=attrs;this.children=[];this.listeners={};this.isConnected=true;this.scrollTop=0;this.disabled='disabled' in attrs;this.value=attrs.value||'';this.textContent='';
      this.classes=new Set((attrs.class||'').split(/\s+/));this.dataset={};
      for (const [name,value] of Object.entries(attrs)) if(name.startsWith('data-'))this.dataset[name.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;
      this.classList={toggle:(name,on)=>{if(on)this.classes.add(name);else this.classes.delete(name);}};
    }
    set innerHTML(html) {
      this.html=String(html);
      this.children=[];const stack=[this];
      for (const token of String(html).matchAll(/<\/?([a-z][\w-]*)\b([^>]*)>/gi)) {
        const name=token[1].toLowerCase();
        if(token[0].startsWith('</')) {if(stack.length>1&&stack.at(-1).tagName===name.toUpperCase())stack.pop();continue;}
        const attrs={};for(const attr of token[2].matchAll(/([\w-]+)(?:="([^"]*)"|='([^']*)')?/g))attrs[attr[1]]=decode(attr[2]??attr[3]??'');
        const parent=stack.at(-1),child=new Element(name,parent,attrs);parent.children.push(child);
        if(!['input','br','hr','col','meta','img','link'].includes(name)&&!token[0].endsWith('/>'))stack.push(child);
      }
    }
    querySelectorAll(selector) {
      const result=[];for(const child of this.children){if(selector.split(',').some(s=>matches(child,s.trim())))result.push(child);result.push(...child.querySelectorAll(selector));}return result;
    }
    querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
    closest(selector){let n=this;while(n){if(selector.split(',').some(s=>matches(n,s.trim())))return n;n=n.parent;}return null;}
    contains(node){while(node){if(node===this)return true;node=node.parent;}return false;}
    get elements(){return Object.fromEntries(this.querySelectorAll('input').map(x=>[x.attrs.name,x]));}
    setAttribute(name,value){this.attrs[name]=value;}
    reportValidity(){return true;}
    addEventListener(name,fn){this.listeners[name]=fn;}
    removeEventListener(name,fn){if(this.listeners[name]===fn)delete this.listeners[name];}
    replaceChildren(){this.children=[];}
  }
  const root=new Element(),calls=[],toasts=[],env={failure:null,managerToday:'2026-11-16',managerTimezone:'Asia/Seoul',foodToday:'2026-11-16',items:[{id:'food-one',name:'합성 식품',quantity:2,expiry_date:'2026-11-20',version:1}]};
  let serial=0;
  const sandbox={Intl,Date,Number,String,Math,Promise,Map,Set,Error,AbortController,JSON,structuredClone,
    setTimeout:()=>++serial,clearTimeout(){},crypto:{randomUUID:()=>`synthetic-${++serial}`},confirm:()=>true,
    RoomManager:{getState:()=>({today:env.managerToday,settings:{timezone:env.managerTimezone}}),refresh:async()=>{}},
    fetch:async(url,options={})=>{
      const method=options.method||'GET',body=options.body?JSON.parse(options.body):undefined;calls.push({url,method,body,options});
      const reply=(data,status=200)=>({ok:status<400,status,json:async()=>structuredClone(data)});
      assert.ok(url.startsWith('/api/food/items'));
      if(env.failure===401)return reply({},401);
      if(method==='GET')return reply({items:env.items,today:env.foodToday});
      if(env.failure==='abort'){const e=new Error('synthetic timeout');e.name='AbortError';throw e;}
      if(env.failure===422)return reply({detail:[{msg:'합성 입력 오류'}]},422);
      if(method==='POST'){const row={...body,id:'food-new',version:1};env.items.push(row);return reply(row,201);}
      const id=url.split('/').at(-1).split('?')[0],row=env.items.find(x=>x.id===id);
      if(!row)return reply({detail:'없음'},404);
      if(method==='PUT'){if(body.version!==row.version)return reply({detail:'버전 충돌'},409);Object.assign(row,body,{version:row.version+1});return reply(row);}
      if(method==='DELETE'){assert.ok(url.endsWith('?version='+row.version));env.items=env.items.filter(x=>x.id!==id);return reply({deleted:true});}
      throw Error('Unexpected mutation');
    }};
  sandbox.window=sandbox;vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(ROOT,'web/shared.js'),'utf8'),sandbox);sandbox.Room.toast=s=>toasts.push(s);
  vm.runInContext(fs.readFileSync(path.join(ROOT,'web/food-expiry.js'),'utf8'),sandbox);
  sandbox.RoomFood.mount(root);await sandbox.RoomFood.refresh();
  const form=()=>root.querySelector('form'),field=name=>form().elements[name],feedback=()=>root.querySelector('[data-food-feedback]').textContent;
  const fill=(name,value)=>{field(name).value=value;form().oninput();};
  const submit=()=>form().onsubmit({preventDefault(){},currentTarget:form()});
  const press=async(op,id)=>{const target=root.querySelectorAll('[data-food-op]').find(x=>x.dataset.foodOp===op&&(!id||x.dataset.id===id));assert.ok(target,op);await root.listeners.click({target});};
  const listHTML=()=>root.querySelector('[data-food-list]').html;
  env.items[0].expiry_date='2026-11-16';env.foodToday='2026-11-17';await sandbox.RoomFood.refresh();
  check('Food response date wins when manager snapshot is still yesterday',listHTML().includes('기한 1일 지남')&&!listHTML().includes('오늘까지'));
  env.managerToday='2026-11-18';await press('new');
  check('Later manager date updates supersede a retained food snapshot',listHTML().includes('기한 2일 지남'));
  env.managerToday='2026-11-16';env.managerTimezone='Pacific/Honolulu';await press('new');
  check('Manager timezone changes may move the civil day backwards',listHTML().includes('오늘까지'));
  env.foodToday=undefined;await sandbox.RoomFood.refresh();
  check('Food responses without a date retain the manager date fallback',listHTML().includes('오늘까지'));
  env.managerTimezone='Asia/Seoul';env.foodToday='2026-11-16';env.items[0].expiry_date='2026-11-20';await sandbox.RoomFood.refresh();
  fill('name','합성 우유');fill('quantity','3');fill('expiry_date','2026-11-18');
  await sandbox.RoomFood.refresh();
  check('Editor refresh does not replace an active draft',field('name').value==='합성 우유'&&field('quantity').value==='3');
  env.failure=422;await submit();
  check('422 errors stay visible without clearing the form',feedback().includes('합성 입력 오류')&&field('name').value==='합성 우유');
  env.failure='abort';await submit();
  check('Ambiguous save outcome keeps draft and asks for state verification',feedback().includes('저장 결과를 확인하지 못했습니다')&&field('quantity').value==='3');
  env.failure=null;await submit();
  const posts=calls.filter(x=>x.method==='POST');
  check('Manual same-content retries reuse one stable create ID',posts.length===3&&new Set(posts.map(x=>x.body.request_id)).size===1);
  check('Create uses numeric count and existing CSRF boundary',posts.every(x=>x.options.headers['X-Room-Request']==='1'&&x.options.credentials==='same-origin')&&posts[0].body.quantity===3);
  check('Successful create clears draft only after server acknowledgement',field('name').value===''&&env.items.length===2);
  await press('edit','food-one');fill('quantity','7');env.items[0].version=2;env.items[0].quantity=5;
  await sandbox.RoomFood.refresh();
  check('Refresh detects stale version and preserves typed values',field('quantity').value==='7'&&root.querySelector('[type=submit]').disabled&&root.querySelector('[data-food-op=delete]').disabled);
  await press('reload-item');
  check('Only explicit reload adopts the current version/values',field('quantity').value==='5'&&!root.querySelector('[type=submit]').disabled);
  fill('quantity','8');env.items[0].version=3;await submit();
  check('409 update preserves draft and blocks repeat writes',feedback().includes('버전 충돌')&&field('quantity').value==='8'&&root.querySelector('[type=submit]').disabled);
  await press('reload-item');fill('quantity','6');await submit();
  check('Reconfirmed edit sends current version',calls.filter(x=>x.method==='PUT').at(-1).body.version===3&&env.items[0].quantity===6);
  await press('edit','food-one');await press('delete');
  check('Deletion sends explicit version and clears selection',calls.filter(x=>x.method==='DELETE').at(-1).url.endsWith('?version=4')&&field('name').value==='');
  env.failure=401;await sandbox.RoomFood.refresh();
  check('Lost manager authentication clears prior food data and draft',root.children.length===0&&toasts.some(x=>x.includes('관리자 로그인이 필요')));
}

editorRegression().then(()=>{
  const report = {passed:checks.length,checks,scope:'Production widget/editor JS in a Node VM; synthetic DOM/API, no browser, server, microphone or paid API.'};
  const folder = path.join(ROOT,'artifacts/test-results'); fs.mkdirSync(folder,{recursive:true});
  fs.writeFileSync(path.join(folder,'food-expiry-regression.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({passed:checks.length,scope:report.scope}));
}).catch(error=>{console.error(error);process.exitCode=1;});
