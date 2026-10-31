/* 纯本地判定：无请求、无补粉；使用本批响应的 age_days 固定比较时间。 */
const ViralFilter = (() => {
  const names = ['最严', '较严', '适中', '宽松', '最松'];
  const keys = ['max_fans', 'min_interactions', 'min_multiplier', 'max_age_days'];
  const values = {
    douyin: [[30000,24000,5,7],[50000,12000,3,14],[50000,3600,1,14],[50000,1200,.3,14],[100000,360,.1,30]],
    xiaohongshu: [[5000,2000,2,7],[10000,1000,1,14],[10000,500,.5,14],[10000,300,.1,14],[30000,100,.05,30]]
  };
  const preset = (p, t) => Object.fromEntries(keys.map((k, i) => [k, values[p][t][i]]));
  function score(row, rules) {
    // 旧预览也从原始赞/藏重算，避免沿用点赞口径的 I/M。
    const hasLikes = Object.prototype.hasOwnProperty.call(row, 'likes');
    const missingCollect = row.platform === 'douyin' && row.collects == null;
    const interactions = hasLikes ? (row.likes == null || (row.platform === 'xiaohongshu' && row.collects == null)
      ? null : row.likes + (row.collects || 0)) : row.interactions;
    const multiplier = row.fans == null || interactions == null ? null : interactions / Math.max(row.fans, 1);
    row = {...row, interactions, multiplier, metric_notes: missingCollect ? ['收藏缺失'] : []};
    const reasons = [];
    if (row.fans == null) reasons.push('粉丝待核');
    else if (row.fans > rules.max_fans) reasons.push('粉丝超上限');
    if (row.interactions == null) reasons.push('互动待核');
    else if (row.interactions < rules.min_interactions) reasons.push('互动不足');
    if (row.multiplier != null && row.multiplier < rules.min_multiplier) reasons.push('倍数不足');
    if (row.age_days == null) reasons.push('日期待核');
    else if (row.age_days < 0) reasons.push('日期异常');
    else if (row.age_days > rules.max_age_days) reasons.push('超出本地天数');
    return {...row, passed: !reasons.length, fail_reasons: reasons,
      pending: row.fans == null || row.interactions == null || row.age_days == null};
  }
  // 营销词只提示疑似，不能当作广告事实。维护词表只改此处。
  const COMMERCE_WORDS = Object.freeze(['去同款App', '同款', '链接', '下载', '购物车', '小黄车', '优惠', '推广', '广告', '合作']);
  const splitWords = text => [...new Set(String(text || '').split(/[,，\s]+/u).filter(Boolean).map(x => x.toLowerCase()))];
  const authorKey = row => row.author_id ? `${row.platform}:${row.author_id}` : '';
  function classify(row, options = {}, feedback = {}) {
    const text = [row.title, row.description, row.topics].filter(Boolean).join(' ').toLowerCase();
    const excluded = splitWords(options.exclude).filter(word => text.includes(word));
    const commerce = COMMERCE_WORDS.filter(word => text.includes(word.toLowerCase()));
    for (const field of row.commerce_fields || []) commerce.push(`平台字段：${field}`);
    const mark = feedback.items?.[row.key] || '';
    const blocked = feedback.authors?.[authorKey(row)] === '屏蔽';
    const reasons = [...excluded.map(x => `排除词：${x}`), ...(blocked ? ['作者已屏蔽'] : [])];
    let group, reason;
    if (mark === '值得跟') { group = '值得跟'; reason = '你已手动标记值得跟'; }
    else if (mark === '不相关') { group = '跳过'; reason = '你已标记不相关'; }
    else if (reasons.length) { group = '跳过'; reason = reasons.join('、'); }
    else if (options.filterCommerce !== false && commerce.length) { group = '跳过'; reason = '疑似带货，建议先核实'; }
    // 已知失败优先于未知；只有缺数据、没有已知失败才归待核参考。
    else if ((row.fail_reasons || []).some(x => !x.endsWith('待核'))) { group = '跳过'; reason = row.fail_reasons.filter(x => !x.endsWith('待核')).join('、'); }
    else if (!row.passed) { group = '可参考'; reason = (row.fail_reasons?.length ? row.fail_reasons : ['数据待核']).join('、'); }
    else {
      group = row.r?.value != null && row.r.grade === '明显爆' && row.r.value >= 10 ? '值得跟' : '可参考';
      const number = n => n == null ? '待核' : n >= 10000 ? `${Number((n / 10000).toFixed(1))} 万` : String(n);
      reason = `粉 ${number(row.fans)}、互动 ${number(row.interactions)}、` +
        (row.r?.value != null ? (row.r.value >= 3 ? `比平时爆 ${row.r.value} 倍` : `为平时的 ${row.r.value} 倍`) : row.r ? row.r.grade : '尚未计算与平时的对比');
    }
    return {...row, mark, blocked, excluded, commerce, review_reasons: reasons, group, reason};
  }
  function sortRows(rows, sort = 'r') {
    const ascending = sort === 'fans';
    const value = row => sort === 'r' ? row.r?.value : sort === 'published_at' ? (row.age_days == null ? null : -row.age_days) : row[sort];
    return [...rows].sort((a, b) => {
      const x = value(a), y = value(b);
      if (x == null || y == null) return x == null ? (y == null ? (b.interactions ?? -1) - (a.interactions ?? -1) : 1) : -1;
      return (ascending ? x - y : y - x) || (b.interactions ?? -1) - (a.interactions ?? -1);
    });
  }
  function sections(rows, byPlatform = false) {
    const result = [];
    for (const group of ['值得跟', '可参考', '跳过']) {
      for (const platform of byPlatform ? ['douyin', 'xiaohongshu'] : [null]) {
        const items = rows.filter(row => row.group === group && (!platform || row.platform === platform));
        if (items.length) result.push({group, platform, items});
      }
    }
    return result;
  }
  const selectable = (row, forced = false) => !row.local && (row.group !== '跳过' || forced);
  return {selectable, names, keys, preset, score, COMMERCE_WORDS, splitWords, authorKey, classify, sortRows, sections};
})();
if (typeof module !== 'undefined') module.exports = ViralFilter;
