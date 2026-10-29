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
  {key:'a',platform:'douyin',title:'灵感',fans:1000,interactions:1500,multiplier:1.5,age_days:1,url:'https://www.douyin.com/video/1',cover:'https://example.com/a.jpg',source:'douyin:search'},
  {key:'b',platform:'douyin',fans:null,interactions:15000,multiplier:null,age_days:1},
  {key:'c',platform:'douyin',fans:1000,interactions:15000,multiplier:15,age_days:null}
]};
refilterViral();
if (viralRows.length !== 1 || !$('viralCounts').textContent.includes('待核 2')) throw Error('宽松/待核统计');
if (!$('viralGrid') || !$('viralGrid').children.length) throw Error('封面网格未渲染');
const cardKids = ($('viralGrid').children || []).flatMap(c => c.children || []);
if (!JSON.stringify($('viralGrid').children).includes('抖音搜索')) throw Error('卡片来源');
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

// 用本地桩接住真实查询请求，验证批量输入和源参数，不访问 TikHub。
(async () => {
  await vm.runInContext(`(async () => {
    const submitted = [];
    api = async (path, options) => { submitted.push({path, body:options.body}); return {job_id:'mock'}; };
    watchJob = (id, callback) => callback({state:'done', viral:{pool:[], scanned:12, calls:9,
      coverage:{keywords:{douyin:['AI','科普']}, publish_time:180,douyin_sources:['search','lowfan'],
        max_pages:3, pages_by_keyword:{douyin:{AI:3, '科普':2}}, source_calls:{douyin:5}}}});
    $('viralSearchSource').checked = true;
    $('viralLowfanSource').checked = true;
    $('viralDouyin').checked = true;
    $('viralXhs').checked = true;
    $('viralKeywords').value = ' AI，科普\\nAI ';
    $('viralSourceWindow').value = '180';
    $('viralMaxPages').value = '3';
    await searchViral({preventDefault(){}});
    if (submitted.length !== 1) throw Error('应只提交一次查询');
    const body = submitted[0].body;
    if (submitted[0].path !== '/api/viral/preview' || body.publish_time !== 180 || JSON.stringify(body.douyin_sources) !== JSON.stringify(['search','lowfan']) || body.max_pages !== 3) throw Error('源参数');
    if (JSON.stringify(body.keywords) !== JSON.stringify(['AI','科普'])) throw Error('批量拆词去重');
    for (const text of ['AI（3页）','科普（2页）','近半年','168 小时','扫描 12 条','TikHub 调用 9 次']) {
      if (!$('viralStatus').textContent.includes(text)) throw Error('摘要缺少：'+text);
    }
    $('viralKeywords').value = Array.from({length:20}, (_,i) => '词'+i).join('，');
    await searchViral({preventDefault(){}});
    if (submitted.length !== 2 || submitted[1].body.keywords.length !== 20) throw Error('20词应可提交');
    $('viralKeywords').value += '，第21词';
    await searchViral({preventDefault(){}});
    if (submitted.length !== 2 || !$('viralStatus').textContent.includes('最多 20')) throw Error('21词应拒绝');
    $('viralKeywords').value = '词'.repeat(81);
    await searchViral({preventDefault(){}});
    if (submitted.length !== 2) throw Error('超长关键词应拒绝');
    $('viralKeywords').value = 'AI';
    $('viralSearchSource').checked = false; $('viralLowfanSource').checked = false;
    await searchViral({preventDefault(){}});
    if (submitted.length !== 2 || !$('viralStatus').textContent.includes('来源')) throw Error('未选来源应拒绝');
    $('viralSearchSource').checked = true;
    await searchViral({preventDefault(){}});
    if (JSON.stringify(submitted[2].body.douyin_sources) !== JSON.stringify(['search'])) throw Error('单搜索来源');
    viralBatch = {coverage:{publish_time:7},pool:[{key:'old',platform:'douyin',fans:1000,interactions:10000,multiplier:10,age_days:8}]};
    $('viralShowPool').checked = false; refilterViral();
    if (viralRows.length) throw Error('拖条不可绕过已查询发布时间范围');

  })()`, context);
  assert.equal(requests, 0);
  console.log('batch query controls and summary: OK; network requests: 0');
})().catch(error => { console.error(error); process.exitCode = 1; });
