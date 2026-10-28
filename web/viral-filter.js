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
  return {names, keys, preset, score};
})();
if (typeof module !== 'undefined') module.exports = ViralFilter;
