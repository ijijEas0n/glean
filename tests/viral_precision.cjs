// 纯规则测试，不创建网络客户端。
const assert = require('assert/strict');
const f = require('../web/viral-filter.js');
const base = {key:'douyin:1', platform:'douyin', id:'1', title:'AI 教程', author_id:'u',passed:true,fans:1000,interactions:2000};
assert.deepEqual(f.splitWords('娱乐， 游戏  娱乐\nAI,ai'), ['娱乐','游戏','ai']);
for (const field of ['title','description','topics']) {
  const row = f.classify({...base,[field]:'娱乐 AI'}, {exclude:'娱乐,游戏'});
  assert.equal(row.group,'跳过');
  assert.deepEqual(row.review_reasons,['排除词：娱乐']);
}
for (const word of f.COMMERCE_WORDS) {
  const row = f.classify({...base,description:word.toLowerCase()});
  assert.equal(row.group,'跳过',word);
  assert.ok(row.commerce.includes(word));
  assert.equal(f.classify({...base,description:word},{filterCommerce:false}).group,'可参考');
}
assert.equal(f.classify({...base,commerce_fields:['promotion']}).group,'跳过');
assert.equal(f.classify(base).group,'可参考');
assert.equal(f.classify({...base,title:'娱乐 广告'},{exclude:'娱乐'}).group,'跳过');
const feedback = {items:{'douyin:1':'不相关'}, authors:{'douyin:u':'屏蔽'}};
const marked = f.classify(base,{},feedback);
assert.equal(marked.mark,'不相关');
assert.equal(marked.blocked,true);
assert.equal(marked.group,'跳过');
assert.equal(f.classify({...base,platform:'xiaohongshu',key:'xiaohongshu:1'}, {},feedback).blocked,false);
assert.equal(f.classify({...base,author_id:''},{},{authors:{'douyin:':'屏蔽'}}).blocked,false);
const rows = [
  {id:'a',multiplier:2,interactions:40,age_days:3,fans:0},
  {id:'b',multiplier:5,interactions:10,age_days:1,fans:100},
  {id:'c',multiplier:null,interactions:null,age_days:null,fans:null}
];
for (const [sort, expected] of [['multiplier','bac'],['interactions','abc'],['published_at','bac'],['fans','abc']])
  assert.equal(f.sortRows(rows,sort).map(x=>x.id).join(''),expected);
assert.equal(rows[0].id,'a'); // 排序不改原池。
const classified = [f.classify(base), f.classify({...base,platform:'xiaohongshu'}),
  f.classify({...base,title:'广告'}), f.classify({...base,title:'游戏'}, {exclude:'游戏'})];
assert.deepEqual(f.sections(classified).map(x=>[x.group,x.items.length]),[['可参考',2],['跳过',2]]);
assert.deepEqual(f.sections(classified,true).map(x=>[x.group,x.platform]),[
  ['可参考','douyin'],['可参考','xiaohongshu'],['跳过','douyin']]);
console.log('precision rules: OK; TikHub requests: 0');

// 人工反馈 > 排除/屏蔽 > 带货 > 数据；R 边界和待核。
const all = {exclude:'AI'};
const manual = {items:{'douyin:1':'值得跟'},authors:{'douyin:u':'屏蔽'}};
assert.equal(f.classify({...base,title:'AI 广告',passed:false}, all, manual).group,'值得跟');
assert.equal(f.classify({...base,r:{value:18,grade:'明显爆'}}, all).group,'跳过');
assert.equal(f.classify({...base,r:{value:18,grade:'明显爆'}}).group,'值得跟');
for (const r of [undefined,{value:null,grade:'样本不足'},{value:null,grade:'基线为零'},{value:2,grade:'平常'},{value:9.99,grade:'偏爆'},{value:10,grade:'偏爆'}])
  assert.equal(f.classify({...base,r}).group,'可参考');
assert.equal(f.classify({...base,r:{value:10,grade:'明显爆'}}).group,'值得跟');
assert.equal(f.classify({...base,passed:false,pending:true,fail_reasons:['粉丝待核']}).group,'可参考');
assert.equal(f.classify({...base,passed:false,pending:true,fail_reasons:['粉丝待核','互动不足']}).group,'跳过');
assert.equal(f.classify({...base,passed:false,fail_reasons:['日期异常']}).group,'跳过');
assert.equal(f.classify({...base,passed:false,fail_reasons:['互动不足'],r:{value:18,grade:'明显爆'}}).group,'跳过');
assert.equal(f.selectable({group:'跳过'}),false);
assert.equal(f.selectable({group:'跳过'},true),true);
assert.equal(f.selectable({group:'跳过',local:true},true),false);
assert.deepEqual(f.sortRows([{id:'a',interactions:3},{id:'b',interactions:5},{id:'c',r:{value:2},interactions:1},{id:'d',r:{value:2},interactions:8}]).map(x=>x.id),['d','c','b','a']);
console.log('三档优先级、待核、R边界、强制入库与排序：通过');
