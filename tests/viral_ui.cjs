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
let requests = 0;
const context = vm.createContext({ document: {getElementById: id => elements.get(id), createElement: tag => new Element(tag)},
  fetch: () => { requests++; throw new Error('禁止网络'); }, console, URL, Set, Map, AbortController, setTimeout, clearTimeout });
vm.runInContext(fs.readFileSync('web/viral-filter.js', 'utf8'), context);
const app = fs.readFileSync('web/app.js', 'utf8');
vm.runInContext(app.slice(0, app.lastIndexOf('\nbind();')), context);
vm.runInContext(`
initViralControls();
if ($('viralTierName').textContent !== '宽松') throw Error('默认档');
viralBatch = {scanned:4, pool:[
  {key:'a',platform:'douyin',title:'灵感',fans:1000,interactions:1500,multiplier:1.5,age_days:1,url:'https://www.douyin.com/video/1',cover:'https://example.com/a.jpg'},
  {key:'b',platform:'douyin',fans:null,interactions:15000,multiplier:null,age_days:1},
  {key:'c',platform:'douyin',fans:1000,interactions:15000,multiplier:15,age_days:null}
]};
refilterViral();
if (viralRows.length !== 1 || !$('viralCounts').textContent.includes('待核 2')) throw Error('宽松/待核统计');
if (!$('viralGrid') || !$('viralGrid').children.length) throw Error('封面网格未渲染');
const cardKids = ($('viralGrid').children || []).flatMap(c => c.children || []);
if (!cardKids.some(ch => ch.tag === 'img' && String(ch.className || '').includes('preview-cover'))) throw Error('应显示封面图');
viralSelected.add('a');
$('viralTier').value = '1'; $('viralTier').events.input();
if (viralRows.length || viralSelected.size) throw Error('严选0/隐藏选择清理');
if (!$('viralStatus').textContent.includes('不能据此判断')) throw Error('零结果说明');
$('viralTier').value = '3'; $('viralTier').events.input();
if (viralRows.length !== 1) throw Error('同批宽松');
$('viralShowPool').checked = true; $('viralShowPool').events.change();
if (viralRows.length !== 3) throw Error('本批参考');
$('viral-douyin-min_interactions').value = '99999'; $('viral-douyin-min_interactions').events.input();
if ($('viralTierName').textContent !== '自定义') throw Error('自定义标签');
$('viralTier').events.input();
if ($('viral-douyin-min_interactions').value != 1000 || $('viralTierName').textContent !== '宽松') throw Error('拖条覆盖');
markViralLocal(viralRows.find(x => x.key === 'a')); refilterViral();
if (!viralRows.find(x => x.key === 'a').local) throw Error('同批重筛保留入库状态');
$('viralShowPool').checked = false;
$('viral-douyin-min_interactions').value = ''; $('viral-douyin-min_interactions').events.input();
if (viralRows.length || !$('viralCounts').textContent.includes('有效')) throw Error('非法输入');
$('viralTier').events.input();
viralBatch = {candidates:[]}; refilterViral();
if (!$('viralCounts').textContent.includes('重新查询')) throw Error('旧响应提示');
`, context);
assert.equal(requests, 0);
console.log('slider interactions: OK; network requests: 0');
