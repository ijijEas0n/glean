// 禁网 DOM 桩：执行真实页面事件绑定，验证拖条/手改/参考视图均不发请求。
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const elements = new Map();
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.value = ''; this.checked = false; }
  set id(v) { this._id = v; elements.set(v, this); }
  get id() { return this._id; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(k, v) { this[k] = v; }
  addEventListener(k, fn) { this.events[k] = fn; }
  querySelectorAll(tag) { return this.children.filter(x => typeof x === 'object').flatMap(x => [...(x.tag === tag ? [x] : []), ...x.querySelectorAll(tag)]); }
  checkValidity() { return [...elements.values()].filter(x => x.id?.startsWith('viral-')).every(x => x.value !== '' && Number.isFinite(Number(x.value)) && Number(x.value) >= 0); }
}
for (const id of fs.readFileSync('web/index.html', 'utf8').matchAll(/id="([^"]+)"/g)) {
  const e = new Element('div'); e.id = id[1];
}
elements.get('viralTier').value = '3';
elements.get('viralFilterCommerce').checked = true;
elements.get('viralSort').value = 'multiplier';
const storage = new Map();
const localStorage = {getItem: k => storage.get(k) || null, setItem: (k, v) => storage.set(k, v)};
let requests = 0;
const context = vm.createContext({ document: {getElementById: id => elements.get(id), createElement: tag => new Element(tag)},
  fetch: () => { requests++; throw new Error('禁止网络'); }, console, localStorage, URL, Set, Map, AbortController, setTimeout, clearTimeout });
vm.runInContext(fs.readFileSync('web/viral-filter.js', 'utf8'), context);
const app = fs.readFileSync('web/app.js', 'utf8');
vm.runInContext(app.slice(0, app.lastIndexOf('\nbind();')), context);

(async () => {
  await vm.runInContext(`(async () => {
    initViralControls();
    const calls = [];
    api = async (path, options) => {
      calls.push({path, body:options.body});
      if (path.endsWith('/estimate')) return {plan_id:'p',authors:1,cached_authors:0,uncached_authors:1,pages:2,estimated_calls:2,budget:20,over_budget:false,missing:['douyin:lost：当前预览无作者 ID']};
      return {calls:1,results:{'douyin:1':{value:12,samples:20,median:100,grade:'明显爆',distribution:'互动范围 0–100',note:'同口径',history_note:'本次近作',fetched_at:1000,cache_hit:false}},errors:{},uncomputed:[]};
    };
    viralFeedback = {items:{'douyin:1':'值得跟','douyin:lost':'值得跟'},authors:{}};
    viralBatch = {pool:[{platform:'douyin',id:'1',key:'douyin:1',author_id:'u',likes:1200,fans:1000,interactions:1200,multiplier:1.2,age_days:1}]};
    refilterViral();
    if (calls.length) throw Error('渲染不得自动计算 R');
    await prepareViralR(null);
    if (calls.length !== 1 || !calls[0].path.endsWith('/estimate')) throw Error('先估算不付费');
    if (!calls[0].body.worth_only || calls[0].body.items[0].author_id !== 'u') throw Error('使用完整预览补作者 ID');
    if ($('viralRConfirm').hidden || !$('viralRStatus').textContent.includes('2 次') || !$('viralRStatus').textContent.includes('lost')) throw Error('确认前展示预计费用和缺失作者');
    $('viralRCancel').events.click();
    await runViralR();
    if (calls.length !== 1) throw Error('取消后不应付费');
    await prepareViralR(viralBatch.pool[0]);
    if (calls[1].body.worth_only || calls[1].body.items.length !== 1) throw Error('单卡只查本条');
    await runViralR();
    if (calls.length !== 3 || calls[2].body.plan_id !== 'p') throw Error('仅确认后执行');
    const text = JSON.stringify($('viralGrid').children);
    if (!text.includes('R 12.00×') || !text.includes('M 1.2×') || !text.includes('中位数 100') || !text.includes('样本 20')) throw Error('R/M 及基线展示');
    const cached = JSON.parse(localStorage.getItem(VIRAL_CACHE_KEY));
    if (cached.batch.pool[0].r.value !== 12) throw Error('R 应随预览保存');
    viralBatch = null; restoreViral();
    if (viralBatch.pool[0].r.value !== 12) throw Error('刷新恢复 R');
    if (calls.length !== 3) throw Error('恢复不应自动付费');
    const sorted = ViralFilter.sortRows([{id:'missing'}, {id:'low',r:{value:2}}, {id:'high',r:{value:12}}], 'r');
    if (sorted.map(x=>x.id).join() !== 'high,low,missing') throw Error('按 R 排序缺值末尾');
    api = async () => ({plan_id:'over',authors:11,cached_authors:0,uncached_authors:11,pages:2,estimated_calls:22,budget:20,over_budget:true,missing:[]});
    await prepareViralR(null);
    if (!$('viralRConfirm').hidden || viralRPlan || !$('viralRStatus').textContent.includes('超出预算')) throw Error('超预算禁止提交');
    api = async () => { throw Error('缓存格式错误'); };
    await prepareViralR(null);
    if (!$('viralRStatus').textContent.includes('缓存格式错误') || viralRPlan) throw Error('失败不可假装成功');
  })()`, context);
  assert.equal(requests, 0);
  console.log('R 按需估算、确认、取消、预算、缺作者、展示、排序和恢复：通过；网络 0 次');
})().catch(err => { console.error(err); process.exitCode = 1; });
