/* 纯本地判定：无请求、无补粉；使用本批响应的 age_days 固定比较时间。 */
const ViralFilter = (() => {
  const names = ['最严', '严选', '适中', '宽松', '最松'];
  const keys = ['max_fans', 'min_interactions', 'min_multiplier', 'max_age_days'];
  const values = {
    douyin: [[30000,20000,5,7],[50000,10000,3,14],[50000,3000,1,14],[50000,1000,.3,14],[100000,300,.1,30]],
    xiaohongshu: [[5000,2000,2,7],[10000,1000,1,14],[10000,500,.5,14],[10000,300,.1,14],[30000,100,.05,30]]
  };
  const preset = (p, t) => Object.fromEntries(keys.map((k, i) => [k, values[p][t][i]]));
  function score(row, rules) {
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
    const group = reasons.length ? '已排除' : options.filterCommerce !== false && commerce.length ? '疑似带货' : '候选';
    return {...row, mark, blocked, excluded, commerce, review_reasons: reasons, group};
  }
  function sortRows(rows, sort = 'multiplier') {
    const ascending = sort === 'fans';
    const value = row => sort === 'r' ? row.r?.value : sort === 'published_at' ? (row.age_days == null ? null : -row.age_days) : row[sort];
    return [...rows].sort((a, b) => {
      const x = value(a), y = value(b);
      if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1;
      return ascending ? x - y : y - x;
    });
  }
  function sections(rows, byPlatform = false) {
    const result = [];
    for (const group of ['候选', '疑似带货', '已排除']) {
      for (const platform of byPlatform ? ['douyin', 'xiaohongshu'] : [null]) {
        const items = rows.filter(row => row.group === group && (!platform || row.platform === platform));
        if (items.length) result.push({group, platform, items});
      }
    }
    return result;
  }
  return {names, keys, preset, score, COMMERCE_WORDS, splitWords, authorKey, classify, sortRows, sections};
})();
if (typeof module !== 'undefined') module.exports = ViralFilter;
