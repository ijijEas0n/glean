'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  page: 'collect', config: {}, libraryRoot: '', jobs: [], preview: null, previewUrl: '', previewLimit: 20,
  previewSelected: new Set(), onlyNew: false, items: [], total: 0, offset: 0, tab: 'all',
  filters: { q: '', author: '', authorId: '', platform: '', collection: '' }, creators: [], selected: new Set(), multi: false,
  currentItem: null, detailVideo: null, lastCollectUrls: [], favoriteStatus: {}, settingsStatus: {}, toastTimer: null,
  expandedCreatorId: '', creatorWorks: new Map(), creatorJobs: new Map(), creatorLoadSeq: 0,
  previewSeq: 0, previewWatchStop: null, favoritePollTimer: null,
  configLoaded: false, onceOptionsDirty: false, profileMode: false, collectResolving: false, previewLoading: false,
};

async function api(path, options = {}) {
  const init = { method: options.method || 'GET', cache: 'no-store', headers: { Accept: 'application/json' } };
  const controller = new AbortController();
  init.signal = controller.signal;
  const timeout = setTimeout(() => controller.abort(), options.timeout ?? 15000);
  if (options.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(options.body); }
  try {
    const response = await fetch(path, init);
    const raw = await response.text();
    let data = {}; try { data = raw ? JSON.parse(raw) : {}; } catch { /* keep empty */ }
    if (!response.ok || data.ok === false) {
      const err = new Error(data.error || data.message || `请求失败（${response.status}）`);
      err.status = response.status; err.running = data.running === true; throw err;
    }
    return data;
  } catch (err) {
    if (err.name === 'AbortError') throw new Error('连接超时，请稍后再试');
    throw err;
  } finally { clearTimeout(timeout); }
}
function node(tag, className, text) { const el = document.createElement(tag); if (className) el.className = className; if (text != null) el.textContent = String(text); return el; }
function toast(text, bad = false) {
  const el = node('div', `toast${bad ? ' is-bad' : ''}`, text); $('toastStack').append(el);
  setTimeout(() => { el.classList.add('fade'); setTimeout(() => el.remove(), 250); }, 2500);
}
function date(iso) {
  if (!iso) return '';
  const d = new Date(iso); if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return '今天';
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}
function authorName(row) { return typeof row.author === 'object' ? row.author?.name || '' : row.author || ''; }
function closePopovers() {
  for (const id of ['collectPopover', 'filterPopover', 'multiPopover', 'detailMenu']) { const el = $(id); if (el) el.hidden = true; }
  document.querySelectorAll('.creator-actions, .card-menu').forEach(el => { el.hidden = true; });
}
function titleOf(row) { return String(row.title || row.platform_label || row.platform || ''); }
function platformLabel(key) { return ({ douyin: '抖音', xiaohongshu: '小红书' })[key] || key || ''; }
function jobStatusLabel(job) {
  if (job.mode === 'viral_preview') return '正在筛选低粉候选…';
  const stage = job.progress?.stage || '';
  return ({ resolve: '正在读取内容…', fetch: '正在读取内容…', media: '正在下载图片和视频…',
    transcribe: '正在把说的话变成文字…', store: '正在存到本地…' })[stage] || '准备保存…';
}
function linksFrom(text) {
  const found = String(text || '').match(/https?:\/\/[^\s\u4e00-\u9fff，。！？、；："']+/gi) || [];
  return [...new Set(found.map(x => x.replace(/[，。、）】"']+$/, '')))];
}
function showPage(page) {
  closePopovers();
  state.page = page;
  for (const [name, id] of Object.entries({ collect: 'pageCollect', library: 'pageLibrary', creators: 'pageCreators', settings: 'pageSettings', detail: 'pageDetail', viral: 'pageViral' })) $(id).hidden = name !== page;
  document.querySelectorAll('.nav-link').forEach(a => a.classList.remove('is-active'));
  const active = ({ collect: 'navCollect', library: 'navLibrary', creators: 'navCreators', settings: 'navSettings', detail: 'navLibrary' })[page];
  if (active) $(active).classList.add('is-active');
  if (page === 'collect') renderCollectOverview();
}
function currentOptions() {
  const transcribe = $('onceTranscribe').checked && state.config.transcribe_engine !== 'off';
  return { download_media: true, transcribe, keep_video: $('onceKeep').checked };
}
function syncCount() {
  const urls = linksFrom($('urlInput').value);
  if (state.preview) {
    const selectedItems = [...(state.preview.children_preview || [])].filter(x => state.previewSelected.has(x.id) && !x.local);
    const selected = selectedItems.length;
    const saving = selectedItems.some(x => x.saving);
    $('submitButton').textContent = saving ? '正在保存…' : selected ? `保存 ${selected} 条` : '保存';
    $('submitButton').disabled = !state.configLoaded || state.previewLoading || selected === 0 || saving;
    $('collectCount').textContent = '';
    const saved = (state.preview.children_preview || []).filter(x => x.local).length;
    const previewMeta = $('collectMeta'); previewMeta.replaceChildren();
    if (saved) previewMeta.append(node('span', 'muted', `已有 ${saved} 条`));
    previewMeta.hidden = !saved;
    return;
  }
  const count = state.lastCollectUrls.length && $('urlInput').value.trim() === state.lastCollectInput ? state.lastCollectUrls.length : urls.length;
  const freshCount = Math.max(0, count - (state.lastSavedCount || 0));
  if (!count) $('submitButton').textContent = '保存';
  else if (state.profileMode) $('submitButton').textContent = `保存 ${state.previewSelected.size} 条`;
  else if (state.collectResolving) $('submitButton').textContent = '正在读取…';
  else $('submitButton').textContent = `保存 ${freshCount} 条`;
  $('submitButton').disabled = !state.configLoaded || !count || state.collectResolving || (state.profileMode && state.previewSelected.size === 0) || (!state.profileMode && freshCount === 0);
  const meta = $('collectMeta'); meta.replaceChildren();
  if (state.preview) {
    const label = node('span', '', `${state.preview.author?.name || '博主'} · 勾选要保存的作品`);
    meta.append(label);
  } else if (count) {
    const profiles = state.detectedProfiles || 0;
    if (profiles) { const p = node('span', '', `${profiles} 个主页 · `); const pick = node('button', 'inline-link', '去挑选'); pick.type = 'button'; pick.addEventListener('click', openDetectedProfile); p.append(pick); meta.append(p); }
    if (state.lastSavedCount) meta.append(node('span', 'muted', `已有 ${state.lastSavedCount} 条`));
  }
  meta.hidden = meta.childElementCount === 0;
  $('collectCount').textContent = '';
}
let resolveTimer = null;
async function inspectLinks() {
  const input = $('urlInput').value.trim(); const urls = linksFrom(input);
  const requestId = (state.resolveSeq || 0) + 1; state.resolveSeq = requestId;
  state.lastCollectInput = input; state.lastCollectUrls = urls; state.lastSavedCount = 0; state.detectedProfiles = 0; state.profileToOpen = ''; state.profileMode = false; state.collectResolving = !!urls.length;
  if (!urls.length) { syncCount(); return; }
  syncCount();
  // Resolve is read-only and lets the submit count exclude already saved works.
  let results;
  try {
    results = await Promise.all(urls.map(url => api('/api/resolve', { method: 'POST', body: { url } })));
  } catch (err) {
    if (requestId === state.resolveSeq && $('urlInput').value.trim() === input) {
      state.collectResolving = false; syncCount(); toast('链接暂时读不出来，请重试', true);
    }
    return;
  }
  if (requestId !== state.resolveSeq || $('urlInput').value.trim() !== input) return;
  state.lastCollectUrls = urls;
  state.detectedProfiles = results.filter(x => x?.kind === 'profile').length;
  state.profileMode = state.detectedProfiles > 0;
  state.collectResolving = false;
  state.profileToOpen = results.find(x => x?.kind === 'profile') ? urls[results.findIndex(x => x?.kind === 'profile')] : '';
  state.lastSavedCount = results.filter(x => x?.already_in_library).length;
  syncCount();
  if (urls.length === 1 && state.profileToOpen) beginPreview(state.profileToOpen);
}
function scheduleInspect() { clearTimeout(resolveTimer); resolveTimer = setTimeout(inspectLinks, 450); }
function openDetectedProfile() { if (state.profileToOpen) beginPreview(state.profileToOpen); }

async function submitCollect(ev) {
  ev?.preventDefault();
  if (!state.configLoaded) { toast('正在读取设置，请稍后再保存', true); return; }
  if (state.preview) { await saveSelectedPreview(); return; }
  const input = $('urlInput').value.trim(); const urls = linksFrom(input);
  if (!urls.length) { toast('先粘贴链接', true); $('urlInput').focus(); return; }
  const opts = currentOptions();
  clearTimeout(resolveTimer);
  const requestId = (state.resolveSeq || 0) + 1; state.resolveSeq = requestId;
  state.collectResolving = true; syncCount();
  let resolving = true;
  try {
    const resolved = await Promise.all(urls.map(url => api('/api/resolve', { method: 'POST', body: { url } })));
    if (requestId !== state.resolveSeq || $('urlInput').value.trim() !== input) return;
    resolving = false;
    const profiles = resolved.map((r, i) => r?.kind === 'profile' ? urls[i] : '').filter(Boolean);
    if (profiles.length === 1 && urls.length === 1) { beginPreview(profiles[0]); return; }
    const fresh = urls.filter((url, i) => !resolved[i]?.already_in_library);
    if (!fresh.length) { toast('这些内容都已保存'); return; }
    if (fresh.length === 1) {
      const r = await api('/api/jobs', { method: 'POST', body: { url: fresh[0], options: opts } });
      monitorJob(r.job_id, 'single');
    } else {
      const items = fresh.map(url => ({ url, transcribe: opts.transcribe }));
      const r = await api('/api/jobs', { method: 'POST', body: { items, options: opts } });
      monitorJob(r.job_id, 'batch');
    }
    $('urlInput').value = ''; state.preview = null; syncCount(); toast(`已提交 ${fresh.length} 条`); refreshJobs();
  } catch (err) { if (requestId === state.resolveSeq) toast(resolving ? '链接暂时读不出来，请重试' : err.message, true); }
  finally { if (requestId === state.resolveSeq) { state.collectResolving = false; syncCount(); } }
}

async function beginPreview(url, accountId = '') {
  invalidatePreview();
  const seq = ++state.previewSeq;
  state.previewUrl = url; state.previewLimit = accountId ? 50 : 20; state.preview = null; state.previewSelected.clear(); state.onlyNew = false; state.profileMode = true; state.previewLoading = true;
  $('inlinePreview').hidden = false; $('previewGrid').replaceChildren(); $('previewProblem').hidden = true; $('onlyNew').checked = false;
  $('collectLower').hidden = true; $('previewShortcuts').hidden = false; $('urlInput').value = url; syncCount(); showPage('collect');
  if (!accountId) {
    const cached = await findProfileCache(url);
    if (seq !== state.previewSeq) return;
    if (cached) {
      state.preview = cached;
      state.previewLimit = Math.max(20, cached.children_preview?.length || 0);
      renderPreview();
      state.previewLoading = false; syncCount();
      const fetched = Date.parse(cached.fetched_at || '');
      if (!fetched || Date.now() - fetched > 12 * 60 * 60 * 1000) {
        state.previewLoading = true; syncCount();
        await requestPreview(state.previewLimit, '', seq);
      }
      return;
    }
  }
  await requestPreview(state.previewLimit, accountId, seq);
}
function normalizedProfileUrl(value) {
  try { const url = new URL(value); return `${url.hostname.toLowerCase()}${url.pathname.replace(/\/$/, '')}`; }
  catch { return String(value || '').split(/[?#]/)[0].replace(/\/$/, '').toLowerCase(); }
}
async function findProfileCache(url) {
  try {
    const [resolved, accounts] = await Promise.all([
      api('/api/resolve', { method: 'POST', body: { url }, timeout: 15000 }), api('/api/accounts')
    ]);
    const target = normalizedProfileUrl(resolved.resolved_url || url);
    const account = (accounts.accounts || []).find(row => normalizedProfileUrl(row.url) === target);
    if (account) {
      const data = await api(`/api/accounts/${encodeURIComponent(account.id)}/works`);
      return { ...creatorPreviewFrom(data, account), fetched_at: data.fetched_at || '' };
    }
    const profiles = await api('/api/items?kind=profile&limit=200&order=fetched_at_desc');
    const row = (profiles.items || []).find(item => normalizedProfileUrl(item.url) === target);
    if (!row) return null;
    const detail = await api(`/api/items/${encodeURIComponent(row.id)}`);
    const item = detail.item || {};
    return {
      ...item, id: item.id || row.id, fetched_at: item.fetched_at || row.fetched_at || '',
      children_preview: item.children || item.children_preview || [],
      children_count: (item.children || item.children_preview || []).length,
    };
  } catch { return null; }
}
function invalidatePreview() {
  state.previewSeq += 1;
  if (state.previewWatchStop) state.previewWatchStop();
  state.previewWatchStop = null;
}
async function requestPreview(limit, accountId = '', seq = state.previewSeq) {
  $('loadMore').disabled = true; $('loadMore').textContent = '加载中…'; $('previewTitle').textContent = '加载中… 已取 0 条';
  if (!state.preview) $('previewGrid').replaceChildren(node('div', 'empty-inline', '加载中… 已取 0 条'));
  syncCount();
  try {
    const options = { limit };
    const result = accountId
      ? await api(`/api/accounts/${encodeURIComponent(accountId)}/sync`, { method: 'POST', body: { options } })
      : await api('/api/preview', { method: 'POST', body: { url: state.previewUrl, options } });
    if (seq !== state.previewSeq) return;
    state.previewWatchStop = watchJob(result.job_id, job => {
      if (seq !== state.previewSeq) return;
      if (['done', 'partial'].includes(job.state)) {
        state.previewLoading = false;
        state.preview = job.partial || job.item || state.preview;
        state.previewLimit = limit; renderPreview(); $('loadMore').disabled = false; $('loadMore').textContent = '加载更多';
        if (accountId) { state.previewSelected.clear(); (state.preview.children_preview || []).forEach(c => { if (!c.local) state.previewSelected.add(c.id); }); renderPreview(); }
      } else if (job.state === 'failed') {
        state.previewLoading = false;
        $('previewProblem').hidden = false; $('previewProblem').textContent = job.error || '作品列表没读出来';
        $('loadMore').disabled = false; $('loadMore').textContent = '加载更多';
      } else {
        const count = Number(job.partial?.children_count || 0);
        $('previewTitle').textContent = `加载中… 已取 ${count} 条`;
        if (job.partial) { state.preview = job.partial; renderPreview(); $('previewTitle').textContent = `加载中… 已取 ${count} 条`; }
      }
    });
  } catch (err) {
    if (seq !== state.previewSeq) return;
    state.previewLoading = false; $('previewProblem').hidden = false; $('previewProblem').textContent = '作品列表暂时读不出来，请重试'; $('loadMore').disabled = false; syncCount();
  }
}
function coverUrl(url, ref) {
  if (!url) return '';
  return `/api/thumb?url=${encodeURIComponent(url)}&ref=${encodeURIComponent(ref || '')}&w=420`;
}
function renderPreview() {
  const p = state.preview; if (!p) return;
  $('previewTitle').textContent = `${p.author?.name || '博主'} · 勾选要保存的作品`;
  const all = p.children_preview || [];
  const total = Number(p.profile?.aweme_count || p.profile?.note_count || p.children_count || 0);
  $('previewTotal').textContent = total ? `共 ${total} 条` : '';
  const show = state.onlyNew ? all.filter(x => !x.local) : all;
  const grid = $('previewGrid'); grid.replaceChildren();
  if (!show.length && state.onlyNew) grid.append(node('div', 'empty-inline', '没有新作品'));
  for (const item of show) {
    const card = node('button', `preview-card${state.previewSelected.has(item.id) ? ' is-selected' : ''}`); card.type = 'button';
    if (item.cover) { const img = node('img', 'preview-cover'); img.loading = 'lazy'; img.alt = ''; img.src = coverUrl(item.cover, state.previewUrl); img.addEventListener('error', () => { img.hidden = true; }); card.append(img); }
    else card.append(node('div', 'preview-cover cover-empty'));
    const check = node('span', 'card-check', state.previewSelected.has(item.id) ? '✓' : ''); card.append(check);
    if (item.local || item.saving) card.append(node('span', 'saved-mark', item.saving ? '已提交' : '已存'));
    card.append(node('span', 'preview-title', item.title || ''));
    card.addEventListener('click', () => { if (item.local || item.saving) return; if (state.previewSelected.has(item.id)) state.previewSelected.delete(item.id); else state.previewSelected.add(item.id); renderPreview(); syncCount(); });
    grid.append(card);
  }
  const hasMore = state.previewLimit < 200 && (total ? all.length < total : all.length >= state.previewLimit);
  $('loadMore').hidden = !hasMore; $('loadMore').disabled = false; $('loadMore').textContent = '加载更多'; syncCount();
}
async function loadPreviewMore() { if (!state.previewUrl) return; invalidatePreview(); const seq = ++state.previewSeq; const want = Math.min(200, state.previewLimit + 20); await requestPreview(want, '', seq); }

async function saveSelectedPreview() {
  const all = state.preview?.children_preview || [];
  const selected = all.filter(x => state.previewSelected.has(x.id) && !x.local);
  if (!state.configLoaded || !selected.length) return;
  if (selected.some(x => x.saving)) return;
  if (!selected.length) { toast('先选要保存的作品', true); return; }
  const opts = currentOptions();
  const selectedIds = new Set(selected.map(x => x.id));
  selected.forEach(x => { x.saving = true; });
  syncCount(); renderPreview();
  try {
    const items = selected.map(c => ({ ...c, transcribe: c.kind === 'video' && opts.transcribe }));
    const r = await api('/api/jobs', { method: 'POST', body: { items, profile_id: state.preview?.id || '', url: state.previewUrl, options: opts } });
    toast(`已提交 ${selected.length} 条`); refreshJobs();
    watchJob(r.job_id, job => {
      const children = job.partial?.children || job.partial?.children_preview || [];
      const terminal = ['done', 'partial', 'failed'].includes(job.state);
      if (terminal) {
        for (const previewItem of state.preview?.children_preview || []) {
          if (!selectedIds.has(previewItem.id)) continue;
          const saved = children.find(child => child.id === previewItem.id || child.url === previewItem.url);
          previewItem.saving = false;
          if (saved?.local || saved?.item_id) {
            previewItem.local = true;
            previewItem.item_id = saved.item_id || saved.id;
            state.previewSelected.delete(previewItem.id);
          }
        }
        syncCount(); renderPreview();
        if (job.state === 'partial') {
          const count = Number(job.children_ok || 0), failed = Number(job.children_failed || 0);
          toast(count === 0 && failed > 0
            ? batchFailureMessage(job)
            : `已入库，但有提醒：${(job.warnings || []).join('；') || '请查看详情'}`, true);
        } else if (job.state === 'failed') toast(job.error || '保存失败', true);
        else toast('所选作品已入库');
      }
    });
  } catch (err) {
    selected.forEach(x => { x.saving = false; });
    syncCount(); renderPreview(); toast(err.message, true);
  }
}

function fetchedAgo(iso) {
  const time = Date.parse(iso || ''); if (!time) return '还没有缓存';
  const minutes = Math.max(0, Math.floor((Date.now() - time) / 60000));
  if (minutes < 1) return '刚刚更新';
  if (minutes < 60) return `${minutes} 分钟前更新`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前更新`;
  return `${Math.floor(hours / 24)} 天前更新`;
}
function creatorPreviewFrom(data, account) {
  return {
    id: data.profile_id || '', author: { name: account.name || '', id: account.ids?.sec_user_id || account.ids?.user_id || '' },
    profile: {}, children_preview: data.works || [], children_count: (data.works || []).length,
  };
}
function mergeCreatorWorks(cached, fresh) {
  const previous = cached || [];
  return (fresh || []).map(item => {
    const biz = String(item.biz_id || (String(item.id || '').split('-').pop()));
    const saved = previous.find(old => old.id === item.id ||
      (biz && String(old.biz_id || old.id?.split('-').pop() || '') === biz && old.local));
    if (saved?.local) return { ...item, local: true, item_id: saved.item_id || saved.id, cover: item.cover || saved.cover || '' };
    if (saved?.saving) return { ...item, saving: true };
    return item;
  });
}
function renderCreatorWorks(account, panel) {
  if (!panel.isConnected) return;
  const data = state.creatorWorks.get(account.id); if (!data) return;
  const header = panel.querySelector('.creator-works-head');
  header.querySelector('.creator-works-age').textContent = fetchedAgo(data.fetched_at);
  const progress = panel.querySelector('.creator-works-progress');
  progress.textContent = data.progress || '';
  progress.hidden = !data.progress;
  const grid = panel.querySelector('.creator-works-grid'); grid.replaceChildren();
  const works = data.works || [];
  for (const item of works) {
    const card = node('button', `preview-card${state.previewSelected.has(item.id) ? ' is-selected' : ''}`); card.type = 'button';
    if (item.cover) { const img = node('img', 'preview-cover'); img.loading = 'lazy'; img.alt = ''; img.src = item.cover.startsWith('/api/media/') ? item.cover : coverUrl(item.cover, account.url); img.addEventListener('error', () => { img.hidden = true; }); card.append(img); }
    else card.append(node('div', 'preview-cover cover-empty'));
    card.append(node('span', 'card-check', state.previewSelected.has(item.id) ? '✓' : ''));
    if (item.local || item.saving) card.append(node('span', 'saved-mark', item.saving ? '已提交' : '已存'));
    card.append(node('span', 'preview-title', item.title || ''));
    card.addEventListener('click', () => {
      if (item.local || item.saving) return;
      state.previewSelected.has(item.id) ? state.previewSelected.delete(item.id) : state.previewSelected.add(item.id);
      renderCreatorWorks(account, panel);
    });
    grid.append(card);
  }
  const onlyNew = !!data.onlyNew;
  const visibleWorks = onlyNew ? works.filter(item => !item.local && !item.saving) : works;
  if (onlyNew) {
    for (const child of [...grid.children]) if (child.classList.contains('preview-card')) child.remove();
    if (!visibleWorks.length) grid.append(node('div', 'empty-inline', '没有新作品'));
  }
  if (!works.length && !data.progress && !onlyNew) grid.append(node('div', 'empty-inline', '还没有缓存作品'));
  const save = panel.querySelector('.creator-works-save');
  const count = works.filter(item => !item.local && !item.saving && state.previewSelected.has(item.id)).length;
  save.textContent = count ? `保存 ${count} 条` : '保存'; save.disabled = !count || !state.configLoaded;
}
function creatorPanel(account) {
  const panel = node('div', 'creator-works');
  const head = node('div', 'creator-works-head');
  head.append(node('span', 'creator-works-age muted', ''));
  const actions = node('div', 'creator-works-actions');
  const refresh = node('button', 'button secondary small', '刷新'); refresh.type = 'button';
  const onlyNewLabel = node('label', 'switch-row compact creator-only-new', '只选没存的');
  const onlyNew = document.createElement('input'); onlyNew.type = 'checkbox'; onlyNew.checked = !!state.creatorWorks.get(account.id)?.onlyNew;
  onlyNewLabel.append(onlyNew, node('span', 'switch'));
  const save = node('button', 'button primary small creator-works-save', '保存'); save.type = 'button'; save.disabled = true;
  actions.append(onlyNewLabel, refresh, save); head.append(actions);
  panel.append(head, node('div', 'creator-works-progress muted', ''), node('div', 'creator-works-grid preview-grid'));
  refresh.addEventListener('click', () => refreshCreatorWorks(account, panel));
  onlyNew.addEventListener('change', () => {
    const data = state.creatorWorks.get(account.id) || { works: [] }; data.onlyNew = onlyNew.checked;
    state.creatorWorks.set(account.id, data); renderCreatorWorks(account, panel);
  });
  save.addEventListener('click', async () => {
    const data = state.creatorWorks.get(account.id); if (!data) return;
    const selected = (data.works || []).filter(item => !item.local && !item.saving && state.previewSelected.has(item.id));
    if (!selected.length) return;
    selected.forEach(item => { item.saving = true; state.previewSelected.delete(item.id); });
    renderCreatorWorks(account, panel);
    try {
      const opts = currentOptions();
      const items = selected.map(item => ({ ...item, transcribe: item.kind === 'video' && opts.transcribe }));
      const result = await api('/api/jobs', { method: 'POST', body: { items, profile_id: data.profile_id || '', url: account.url, options: opts } });
      toast(`已提交 ${selected.length} 条`); refreshJobs();
      watchJob(result.job_id, async job => {
        if (job.connectionError) return;
        if (!['done', 'partial', 'failed'].includes(job.state)) return;
        try {
          const latest = await api(`/api/accounts/${encodeURIComponent(account.id)}/works`);
          const current = state.creatorWorks.get(account.id) || data;
          current.works = mergeCreatorWorks(current.works, latest.works || []).map(item => ({ ...item, saving: false }));
          state.creatorWorks.set(account.id, current);
          if (panel.isConnected) renderCreatorWorks(account, panel);
        } catch {
          const current = state.creatorWorks.get(account.id) || data;
          current.works = (current.works || []).map(item => selected.some(saved => saved.id === item.id) ? { ...item, saving: false } : item);
          state.creatorWorks.set(account.id, current);
          if (panel.isConnected) renderCreatorWorks(account, panel);
        }
      });
    } catch (err) {
      selected.forEach(item => { item.saving = false; state.previewSelected.add(item.id); });
      renderCreatorWorks(account, panel); toast(err.message, true);
    }
  });
  return panel;
}
async function refreshCreatorWorks(account, panel) {
  if (state.creatorJobs.has(account.id)) return;
  const seq = (state.creatorRefreshSeq?.get(account.id) || 0) + 1;
  if (!state.creatorRefreshSeq) state.creatorRefreshSeq = new Map();
  state.creatorRefreshSeq.set(account.id, seq);
  state.creatorJobs.set(account.id, { seq, stop: null });
  const refreshButton = panel.querySelector('.creator-works-head .button.secondary');
  if (refreshButton) refreshButton.disabled = true;
  const current = state.creatorWorks.get(account.id) || { works: [], fetched_at: '' };
  current.progress = current.works.length ? '正在刷新…' : '加载中… 已取 0 条';
  state.creatorWorks.set(account.id, current); renderCreatorWorks(account, panel);
  try {
    const result = await api(`/api/accounts/${encodeURIComponent(account.id)}/sync`, { method: 'POST', body: { options: { limit: 50 } } });
    if (state.creatorRefreshSeq.get(account.id) !== seq) return;
    const stop = watchJob(result.job_id, job => {
      if (state.creatorRefreshSeq.get(account.id) !== seq) return;
      const latest = state.creatorWorks.get(account.id) || current;
      const count = Number(job.partial?.children_count || 0);
      latest.progress = job.state === 'failed' ? (job.error || '刷新失败')
        : ['done', 'partial'].includes(job.state) ? ''
          : `加载中… 已取 ${count} 条`;
      if (job.partial?.children_preview) latest.works = mergeCreatorWorks(latest.works, job.partial.children_preview);
      if (['done', 'partial'].includes(job.state)) {
        state.creatorJobs.delete(account.id); if (refreshButton) refreshButton.disabled = false;
        latest.fetched_at = job.partial?.fetched_at || new Date().toISOString();
        latest.works = mergeCreatorWorks(latest.works, job.partial?.children_preview || latest.works).map(item => ({ ...item, local: !!item.local }));
        state.previewSelected.clear(); latest.works.forEach(item => { if (!item.local) state.previewSelected.add(item.id); });
      }
      if (job.state === 'failed') { state.creatorJobs.delete(account.id); if (refreshButton) refreshButton.disabled = false; }
      state.creatorWorks.set(account.id, latest);
      const activeRow = [...document.querySelectorAll('.creator-row')].find(row => row.dataset.accountId === account.id);
      const activePanel = activeRow?.querySelector('.creator-works');
      if (activePanel) renderCreatorWorks(account, activePanel);
    });
    const active = state.creatorJobs.get(account.id); if (active?.seq === seq) active.stop = stop;
  } catch (err) { if (state.creatorRefreshSeq.get(account.id) !== seq) return; state.creatorJobs.delete(account.id); if (refreshButton) refreshButton.disabled = false; current.progress = err.message; state.creatorWorks.set(account.id, current); renderCreatorWorks(account, panel); }
}
async function openCreatorWorks(account, row, button) {
  if (state.expandedCreatorId === account.id) {
    row.querySelector('.creator-works')?.remove(); state.expandedCreatorId = ''; button.textContent = '看看'; return;
  }
  document.querySelectorAll('.creator-works').forEach(el => el.remove());
  document.querySelectorAll('.creator-row > .button.primary').forEach(el => { el.textContent = '看看'; });
  state.expandedCreatorId = account.id; state.previewSelected.clear(); button.textContent = '收起';
  const panel = creatorPanel(account); row.append(panel);
  if (!state.creatorWorks.has(account.id)) state.creatorWorks.set(account.id, { works: [], fetched_at: '', progress: '加载中… 已取 0 条', loading: true });
  renderCreatorWorks(account, panel);
  await loadCreatorWorks(account, panel);
}
async function loadCreatorWorks(account, panel) {
  if (!state.creatorLoadSeqMap) state.creatorLoadSeqMap = new Map();
  const loadSeq = (state.creatorLoadSeqMap.get(account.id) || 0) + 1; state.creatorLoadSeqMap.set(account.id, loadSeq);
  try {
    const data = await api(`/api/accounts/${encodeURIComponent(account.id)}/works`);
    if (loadSeq !== state.creatorLoadSeqMap.get(account.id) || state.expandedCreatorId !== account.id || !panel.isConnected) return;
    data.progress = ''; data.loading = false; data.onlyNew = !!state.creatorWorks.get(account.id)?.onlyNew;
    data.works = data.works || [];
    state.creatorWorks.set(account.id, data);
    const preview = creatorPreviewFrom(data, account);
    data.works.forEach(item => { if (!item.local) state.previewSelected.add(item.id); });
    state.preview = preview; state.previewUrl = account.url;
    renderCreatorWorks(account, panel);
    const fetched = Date.parse(data.fetched_at || '');
    if (!fetched || Date.now() - fetched > 12 * 60 * 60 * 1000) refreshCreatorWorks(account, panel);
  } catch (err) { if (loadSeq !== state.creatorLoadSeqMap.get(account.id) || !panel.isConnected) return; state.creatorWorks.set(account.id, { works: [], progress: err.message }); renderCreatorWorks(account, panel); }
}

function watchJob(id, callback) {
  let stopped = false, failures = 0, timer = null;
  const stop = () => { stopped = true; if (timer) clearTimeout(timer); };
  const fail = (message) => { stop(); callback({ state: 'failed', error: message, connectionError: true }); };
  const poll = async () => {
    if (stopped) return;
    try {
      const r = await api(`/api/jobs/${encodeURIComponent(id)}`); const job = r.job;
      failures = 0;
      callback(job);
      if (['done', 'partial', 'failed'].includes(job.state)) { stop(); refreshJobs(); refreshRecent(); return; }
    } catch (e) {
      if (e.status === 404) { fail('连接断了，稍后再试'); return; }
      failures += 1;
      if (failures >= 5) { fail('连接断了，稍后再试'); return; }
    }
    if (!stopped) timer = setTimeout(poll, 1300);
  };
  poll(); return stop;
}
function batchFailureMessage(job) {
  const count = Number(job.children_failed || 0);
  const warnings = (job.warnings || []).join('；');
  const prefix = `0 条入库，${count} 条失败`;
  if (warnings.startsWith(prefix)) return warnings;
  return `${prefix}${warnings ? `：${warnings}` : '，请查看任务详情'}`;
}
function monitorJob(id, kind) { return watchJob(id, job => {
  if (job.state === 'partial' && Number(job.children_ok) === 0 && Number(job.children_failed) > 0)
    toast(batchFailureMessage(job), true);
  else if (job.state === 'partial') toast(`已入库，但有提醒：${(job.warnings || []).join('；') || '请查看详情'}`, true);
  else if (job.state === 'done') toast(kind === 'batch' ? '所选作品已入库' : '已入库');
  else if (job.state === 'failed') toast(job.error || '保存失败', true);
}); }
async function refreshJobs() {
  try { const r = await api('/api/jobs'); state.jobs = r.jobs || []; renderCollectOverview(); } catch { /* local service may restart */ }
}
function activeJobs() { return state.jobs.filter(j => j.mode !== 'preview' && ['queued', 'running'].includes(j.state)); }
function jobDisplayName(job) {
  const profile = job.mode === 'preview' || job.kind === 'profile';
  const author = job.partial?.author?.name || job.item?.author?.name || job.account_name || '';
  const clean = value => String(value || '').replace(/的(?:抖音|小红书)?主页$/, '').trim();
  const title = [job.title, job.partial?.title, job.item?.title].map(value => String(value || '').trim()).find(value => value && !/^https?:\/\//i.test(value)) || '';
  const name = profile ? (author || clean(title)) : title;
  return name && !/^https?:\/\//i.test(name) ? name : (profile ? '博主主页' : '作品');
}
function jobProgressLabel(job) {
  if (job.mode === 'preview') {
    const count = job.partial?.children_count || job.partial?.children_preview?.length || job.progress?.count || 0;
    return `加载中… 已取 ${count} 条`;
  }
  return jobStatusLabel(job);
}
function renderCollectOverview() {
  const jobs = activeJobs();
  $('jobBadge').hidden = !jobs.length; $('jobBadge').textContent = String(jobs.length);
  $('activeCount').textContent = jobs.length ? `${jobs.length}` : '';
  $('shortcutJobCount').textContent = jobs.length ? ` ${jobs.length}` : '';
  const list = $('activeJobs'); list.replaceChildren();
  if (!jobs.length) list.append(node('div', 'empty-inline', '没有进行中的任务'));
  jobs.slice(0, 4).forEach(job => {
    const row = node('div', 'job-row'); const body = node('div', 'job-title-wrap');
    body.append(node('b', 'job-title', jobDisplayName(job)));
    const progress = Math.max(0, Math.min(100, Number(job.progress?.percent || 0)));
    row.append(body, node('span', 'job-stage', jobProgressLabel(job)));
    const track = node('div', 'progress-track'); const fill = node('i'); fill.style.width = `${progress}%`; track.append(fill); row.append(track);
    list.append(row);
  });
  refreshRecent();
}
async function refreshRecent() {
  try {
    const r = await api('/api/items?limit=5&source=library&order=fetched_at_desc'); const list = $('recentItems'); list.replaceChildren();
    const rows = (r.items || []).filter(x => x.kind !== 'profile').slice(0, 5);
    if (!rows.length) list.append(node('div', 'empty-inline', '保存的内容会显示在这里'));
    rows.forEach(row => {
      const a = node('a', 'recent-row'); a.href = `#/item/${encodeURIComponent(row.id)}`; a.append(node('b', 'recent-title', titleOf(row)));
      if (row.kind === 'video' && !row.has_transcript) a.append(node('i', 'missing-dot'));
      a.append(node('span', 'muted', date(row.fetched_at))); list.append(a);
    });
  } catch { /* keep page usable */ }
}

async function loadLibrary(reset = true) {
  if (reset) { state.offset = 0; state.items = []; }
  const params = new URLSearchParams({ limit: '200', offset: String(state.offset), order: 'fetched_at_desc' });
  if (state.tab === 'favorites') params.set('source', 'favorites');
  if (state.filters.q) params.set('q', state.filters.q);
  if (state.filters.platform) params.set('platform', state.filters.platform);
  if (state.filters.collection) params.set('collection', state.filters.collection);
  if (state.filters.author) params.set('author', state.filters.author);
  if (state.filters.authorId) params.set('author_id', state.filters.authorId);
  if (state.tab === 'missing') params.set('missing_transcript', '1');
  params.set('exclude_kind', 'profile');
  try {
    const r = await api(`/api/items?${params}`); const visible = r.items || [];
    state.items = reset ? visible : [...state.items, ...visible]; state.pageTotal = r.total; state.total = r.total;
    state.libraryPageMeta = r; renderLibrary();
  } catch (err) { $('libraryEmpty').hidden = false; $('libraryEmpty').textContent = err.message; }
}
function fillFilterOptions() {
  const groups = state.libraryPageMeta?.author_counts || []; const author = $('authorFilter');
  author.replaceChildren(new Option('全部', ''));
  const nameCounts = new Map(); groups.forEach(group => nameCounts.set(group.name, (nameCounts.get(group.name) || 0) + 1));
  groups.forEach(group => {
    const label = nameCounts.get(group.name) > 1 ? `${group.name} · ${group.id.slice(-6)}` : group.name;
    author.add(new Option(label, group.id));
  });
  author.value = state.filters.authorId;
  const collections = Object.keys(state.libraryPageMeta?.by_collection || {}); const group = $('collectionFilter'); const chosen = state.filters.collection;
  group.replaceChildren(new Option('全部', '')); collections.forEach(a => group.add(new Option(a, a))); group.value = chosen;
}
function renderLibrary() {
  const grid = $('libraryGrid'); grid.replaceChildren();
  state.items.forEach(row => {
    const card = node('article', `library-card${state.selected.has(row.id) ? ' is-selected' : ''}`); card.dataset.id = row.id;
    const cover = row.cover ? node('img', 'library-cover') : node('div', 'library-cover cover-empty');
    if (row.cover) { cover.src = row.cover; cover.loading = 'lazy'; cover.alt = ''; }
    card.append(cover);
    const content = node('div', 'library-card-copy'); const title = node('b', 'library-title', titleOf(row));
    if (row.kind === 'video' && !row.has_transcript) title.append(node('i', 'missing-dot'));
    content.append(title, node('span', 'muted', authorName(row))); card.append(content);
    const actions = node('div', 'card-actions');
    const copy = node('button', 'copy-hover', '复制给 AI'); copy.type = 'button'; copy.addEventListener('click', ev => { ev.preventDefault(); ev.stopPropagation(); copyForAI([row]); }); actions.append(copy);
    const more = node('button', 'card-more', '⋯'); more.type = 'button'; more.setAttribute('aria-label', '更多操作');
    const menu = node('div', 'popover card-menu'); menu.hidden = true;
    const copyMenu = node('button', '', '复制给 AI'); copyMenu.type = 'button'; copyMenu.addEventListener('click', ev => { ev.stopPropagation(); menu.hidden = true; copyForAI([row]); });
    menu.append(copyMenu); more.addEventListener('click', ev => { ev.preventDefault(); ev.stopPropagation(); document.querySelectorAll('.card-menu').forEach(x => { if (x !== menu) x.hidden = true; }); menu.hidden = !menu.hidden; });
    actions.append(more, menu); card.append(actions);
    if (state.multi) { const tick = node('span', 'card-check', state.selected.has(row.id) ? '✓' : ''); card.append(tick); }
    card.addEventListener('click', () => { if (state.multi) { toggleSelected(row.id); return; } location.hash = `#/item/${encodeURIComponent(row.id)}`; });
    grid.append(card);
  });
  $('libraryTotal').textContent = String(state.total || state.items.length);
  $('libraryEmpty').hidden = state.items.length > 0;
  $('libraryMore').hidden = state.offset + 200 >= (state.pageTotal || state.total);
  $('multiBar').hidden = !state.multi; $('selectedCount').textContent = `已选 ${state.selected.size} 条`;
  $('copySelected').disabled = state.selected.size === 0;
  fillFilterOptions();
  $('copyFiltered').hidden = !(state.filters.author || state.filters.collection);
  $('copyFiltered').textContent = `复制这 ${state.total} 条给 AI`;
  renderAuthorChips();
}
function renderAuthorChips() {
  const wrap = $('authorChips'); if (!wrap) return;
  wrap.replaceChildren(); wrap.hidden = state.tab !== 'all';
  if (wrap.hidden) return;
  const groups = state.libraryPageMeta?.author_counts || [];
  const total = Number(state.libraryPageMeta?.author_total || 0);
  const all = node('button', `author-chip${state.filters.authorId || state.filters.author ? '' : ' is-active'}`, `全部 ${total}`);
  all.type = 'button'; all.addEventListener('click', () => { clearSelected(); state.filters.author = ''; state.filters.authorId = ''; $('authorFilter').value = ''; loadLibrary(); }); wrap.append(all);
  groups.forEach(group => {
    const button = node('button', `author-chip${state.filters.authorId === group.id ? ' is-active' : ''}`, `${group.name} ${group.count}`);
    button.type = 'button'; button.title = group.name; button.addEventListener('click', () => {
      clearSelected();
      if (state.filters.authorId === group.id) { state.filters.author = ''; state.filters.authorId = ''; $('authorFilter').value = ''; }
      else { state.filters.author = group.name; state.filters.authorId = group.id; $('authorFilter').value = group.id; }
      loadLibrary();
    }); wrap.append(button);
  });
}
function toggleSelected(id) { if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id); renderLibrary(); }
function clearSelected() { state.selected.clear(); }
function setMulti(on) { state.multi = on; document.body.classList.toggle('is-multi', on); state.selected.clear(); renderLibrary(); }
function selectedRows() { return state.items.filter(row => state.selected.has(row.id)); }
async function copyForAI(rows) {
  const valid = rows.filter(x => x?.id);
  if (!valid.length) { toast('没有可复制的内容', true); return; }
  try {
    if (!state.libraryRoot) state.libraryRoot = (await api('/api/status')).library_root || '';
    const path = row => `${state.libraryRoot.replace(/\/$/, '')}/items/${row.id}/content.md`;
    const value = valid.length === 1 ? path(valid[0]) : `请读这 ${valid.length} 条参考资料：\n${valid.map(row => `${path(row)}  # ${titleOf(row)}`).join('\n')}`;
    await navigator.clipboard.writeText(value); toast('已复制 · 粘给 AI 就能读');
  } catch (err) { toast(err.message || '复制失败', true); }
}
async function copyAllFiltered() {
  try {
    const params = new URLSearchParams({ limit: '200', order: 'fetched_at_desc' });
    if (state.tab === 'favorites') params.set('source', 'favorites');
    if (state.filters.q) params.set('q', state.filters.q);
    if (state.filters.platform) params.set('platform', state.filters.platform);
    if (state.filters.collection) params.set('collection', state.filters.collection);
    if (state.filters.author) params.set('author', state.filters.author);
    if (state.filters.authorId) params.set('author_id', state.filters.authorId);
    if (state.tab === 'missing') params.set('missing_transcript', '1');
    params.set('exclude_kind', 'profile');
    let rows = [], total = 0, offset = 0;
    do {
      params.set('offset', String(offset));
      const page = await api(`/api/items?${params}`); total = page.total || 0;
      const batch = page.items || []; rows.push(...batch); offset += batch.length;
      if (!batch.length) break;
    } while (offset < total);
    await copyForAI(rows);
  } catch (err) { toast(err.message, true); }
}
async function deleteSelected() {
  const ids = [...state.selected]; if (!ids.length) return;
  if (!confirm(`删除选中的 ${ids.length} 条内容？`)) return;
  for (const id of ids) { try { await api(`/api/items/${encodeURIComponent(id)}`, { method: 'DELETE' }); } catch (err) { toast(err.message, true); } }
  setMulti(false); await loadLibrary();
}

async function openDetail(id) {
  try {
    const r = await api(`/api/items/${encodeURIComponent(id)}`); state.currentItem = r.item;
    renderDetail(r.item); showPage('detail');
  } catch (err) { toast(err.message, true); location.hash = '#/library'; }
}
function renderDetail(item) {
  $('detailTitle').textContent = titleOf(item);
  const sources = [authorName(item), platformLabel(item.platform), item.source === 'favorites' ? `收藏夹/${(item.collections || []).join('、') || '未分类'}` : ''].filter(Boolean);
  $('detailBy').textContent = sources.join(' · ');
  $('originLink').href = item.url || '#';
  $('detailText').replaceChildren(); $('detailTranscript').replaceChildren(); $('detailMedia').replaceChildren(); $('detailRaw').hidden = true; state.detailVideo = null;
  const text = String(item.text || '').trim(); if (text) $('detailText').append(node('div', 'prose', text));
  else $('detailText').append(node('div', 'empty-inline', '没有正文'));
  const media = item.media || []; let shownCover = false;
  const hasPlayableVideo = media.some(m => m.type === 'video' && m.path && !m.removed);
  const orderedMedia = [...media].sort((a, b) => Number(b.type === 'video') - Number(a.type === 'video'));
  orderedMedia.forEach(m => {
    const src = m.path ? `/api/media/${encodeURIComponent(item.id)}/${encodeURIComponent(m.path.split('/').pop())}` : m.url;
    if (m.type === 'video' && m.path && !m.removed) { const v = node('video', 'detail-video'); v.controls = true; v.src = src; state.detailVideo = v; $('detailMedia').append(v); }
    else if (m.type === 'image' && !(hasPlayableVideo && m.role === 'cover')) { const img = node('img', 'detail-image'); img.src = src; img.alt = ''; $('detailMedia').append(img); shownCover ||= m.role === 'cover'; }
  });
  const transcript = item.transcript || {}; const segments = (transcript.segments || []).filter(segment => String(segment.text || segment.content || '').trim());
  if (segments.length) {
    const list = node('div', 'transcript-lines');
    for (const segment of segments) {
      const line = node('div', 'transcript-line');
      const rawStart = Number(segment.start ?? segment.start_time ?? segment.begin ?? 0);
      const seconds = rawStart >= 10000 ? rawStart / 1000 : rawStart;
      const stamp = node('button', 'transcript-time', formatTime(seconds)); stamp.type = 'button';
      stamp.addEventListener('click', () => { if (state.detailVideo) { state.detailVideo.currentTime = Math.max(0, seconds); state.detailVideo.play().catch(() => {}); } });
      line.append(stamp, node('span', '', segment.text || segment.content || '')); list.append(line);
    }
    $('detailTranscript').append(list);
  } else if (String(transcript.text || '').trim()) $('detailTranscript').append(node('div', 'prose', transcript.text));
  else $('detailTranscript').append(node('div', 'empty-inline', transcript.note ? `没有文字稿：${transcript.note}` : '没有文字稿'));
  if (item.kind === 'video' && !state.detailVideo) {
    if (!shownCover && item.cover) { const cover = node('img', 'detail-image detail-cover'); cover.src = coverUrl(item.cover, item.url); cover.alt = ''; $('detailMedia').append(cover); }
    const videoMedia = media.find(m => m.type === 'video') || {};
    const message = videoMedia.removed ? '本地视频文件已删除'
      : videoMedia.error ? `视频未下载：${videoMedia.error}`
      : videoMedia.download_skipped ? '本次未下载视频'
      : (item.warnings || []).find(w => /视频.*(?:下载失败|未下载)/.test(w)) || '视频未下载';
    $('detailMedia').append(node('div', 'muted', message));
  }
  const problems = [...new Set([...(item.warnings || []), transcript.note].filter(Boolean))];
  if (item.kind === 'video' && !segments.some(segment => String(segment.text || segment.content || '').trim()) && !String(transcript.text || '').trim()) problems.push('没有文字稿');
  $('detailCompleteness').hidden = !problems.length; $('detailCompleteness').textContent = problems.join(' · ');
  $('detailText').hidden = false; $('detailTranscript').hidden = false; $('detailMedia').hidden = false;
}
function formatTime(seconds) { const s = Math.max(0, Math.floor(Number(seconds) || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
async function copyDetail(kind) {
  const item = state.currentItem; if (!item) return;
  if (kind === 'ai') { await copyForAI([item]); $('copyOne').textContent = '已复制 ✓'; setTimeout(() => { $('copyOne').textContent = '复制给 AI'; }, 2000); return; }
  if (kind === 'text') { await navigator.clipboard.writeText(item.text || ''); toast('正文已复制'); }
  if (kind === 'transcript') { await navigator.clipboard.writeText(item.transcript?.text || ''); toast('文字稿已复制'); }
  if (kind === 'folder') { try { await api('/api/reveal', { method: 'POST', body: { item_id: item.id } }); } catch (err) { toast(err.message, true); } }
  if (kind === 'raw') {
    try {
      const r = await api(`/api/items/${encodeURIComponent(item.id)}/raw`);
      $('detailRaw').textContent = (r.files || []).map(file => `// ${file.name}\n${JSON.stringify(file.data, null, 2)}`).join('\n\n') || '没有接口原始记录';
      $('detailRaw').hidden = false;
    } catch (err) { toast(err.message, true); }
  }
}

async function loadCreators() {
  try { const r = await api('/api/accounts'); state.creators = r.accounts || []; if (!state.creators.some(a => a.id === state.expandedCreatorId)) state.expandedCreatorId = ''; renderCreators(); }
  catch (err) { toast(err.message, true); }
}
function renderCreators() {
  const list = $('creatorList'); list.replaceChildren(); $('creatorEmpty').hidden = state.creators.length > 0;
  state.creators.forEach(a => {
    const p = a.profile || {}; const count = Math.max(0, Number(p.works || 0) - Number(p.local ?? a.local_count ?? 0));
    const row = node('article', 'creator-row'); row.dataset.accountId = a.id;
    const name = a.name || p.name || '博主';
    const avatar = node('div', 'creator-avatar', name.slice(0, 1));
    if (p.avatar) {
      const img = node('img', 'creator-avatar'); img.src = `/api/avatar?url=${encodeURIComponent(p.avatar)}`; img.alt = ''; img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', () => img.replaceWith(avatar), { once: true }); row.append(img);
    } else row.append(avatar);
    row.append(node('b', 'creator-name', name), node('span', count ? 'creator-new' : 'muted', count ? `${count} 条新作品没存` : '都存了'));
    const see = node('button', 'button primary small', state.expandedCreatorId === a.id ? '收起' : '看看'); see.type = 'button'; see.addEventListener('click', () => openCreatorWorks(a, row, see)); row.append(see);
    const menu = node('button', 'button secondary small creator-menu', '⋯'); menu.type = 'button'; menu.addEventListener('click', () => creatorMenu(a, menu)); row.append(menu); list.append(row);
    if (state.expandedCreatorId === a.id) {
      see.textContent = '收起';
      const panel = creatorPanel(a); row.append(panel);
      panel.querySelector('.creator-works-head .button.secondary').disabled = state.creatorJobs.has(a.id);
      renderCreatorWorks(a, panel);
      if (!state.creatorWorks.has(a.id) || state.creatorWorks.get(a.id).loading) loadCreatorWorks(a, panel);
    }
  });
}
function creatorMenu(account, trigger) {
  const open = trigger.parentElement.querySelector('.creator-actions');
  if (open && !open.hidden) { open.remove(); return; }
  open?.remove();
  closePopovers();
  const menu = node('div', 'popover creator-actions');
  const rename = node('button', '', '改名'); const remove = node('button', '', '移除');
  rename.addEventListener('click', async () => {
    const name = prompt('博主名字', account.name || ''); if (name == null) return;
    try { await api(`/api/accounts/${encodeURIComponent(account.id)}`, { method: 'PATCH', body: { name } }); await loadCreators(); }
    catch (err) { toast(err.message, true); }
  });
  remove.addEventListener('click', async () => {
    if (!confirm(`移除「${account.name}」？`)) return;
    try { await api(`/api/accounts/${encodeURIComponent(account.id)}`, { method: 'DELETE' }); await loadCreators(); }
    catch (err) { toast(err.message, true); }
  });
  menu.append(rename, remove); trigger.parentElement.append(menu);
  const rect = trigger.getBoundingClientRect();
  const left = window.innerWidth <= 720 ? rect.left + 46 : Math.min(rect.right + 8, window.innerWidth - 120);
  menu.style.position = 'fixed'; menu.style.left = `${Math.max(8, left)}px`; menu.style.top = `${Math.max(8, rect.top)}px`; menu.style.right = 'auto';
}
async function addCreator() {
  const url = $('creatorUrl').value.trim(); if (!url) { toast('先粘贴主页链接', true); return; }
  try { await api('/api/accounts', { method: 'POST', body: { url, name: $('creatorName').value.trim() } }); $('creatorUrl').value = ''; $('creatorName').value = ''; $('creatorAdd').hidden = true; await loadCreators(); }
  catch (err) { toast(err.message, true); }
}

async function syncFavorites() {
  state.favoriteStatus.running = true; renderFavoriteStatus(state.favoriteStatus);
  try { const r = await api('/api/favorites/sync', { method: 'POST', body: {} }); toast(r.message || '开始同步'); pollFavorites(); }
  catch (err) { if (err.running) { toast('正在同步'); pollFavorites(); return; } state.favoriteStatus.running = false; renderFavoriteStatus(state.favoriteStatus); toast(err.message, true); }
}
function renderFavoriteStatus(status) {
  const running = !!status?.running;
  $('syncFavorites').disabled = running; $('syncFavorites').textContent = running ? '同步中…' : '同步';
  $('shortcutFavorites').disabled = running; $('shortcutFavorites').textContent = running ? '收藏夹 · 同步中…' : '收藏夹 · 同步';
  $('favoriteRetry').disabled = running;
  $('favoriteLast').textContent = running ? '同步中…' : status?.finished_at ? `上次 ${new Date(status.finished_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })}` : '';
}
async function pollFavorites() {
  try {
    const s = await api('/api/favorites/status'); state.favoriteStatus = s;
    renderFavoriteStatus(s);
    const failed = Number(s.failed_count || s.fail_count || 0); $('favoriteRetry').hidden = failed <= 0;
    $('favoriteRetry').textContent = `${failed} 条失败 · 重试`;
    if (state.favoritePollTimer) clearTimeout(state.favoritePollTimer);
    state.favoritePollTimer = s.running ? setTimeout(pollFavorites, 4000) : null;
    return s;
  } catch { return null; }
}

async function loadSettings() {
  try {
    const [cfg, status, fav] = await Promise.all([api('/api/config'), api('/api/status'), api('/api/favorites/status')]);
    state.config = cfg.config || {}; state.settingsStatus = status; state.favoriteStatus = fav;
    renderFavoriteStatus(fav); renderSettings();
  } catch (err) { $('settingsProblems').hidden = false; $('settingsProblems').textContent = err.message; $('settingsDot').hidden = false; }
}
function renderSettings() {
  const c = state.config; $('settingTranscribe').checked = c.transcribe_engine !== 'off'; $('settingKeep').checked = c.keep_video !== false;
  $('settingEngine').value = c.transcribe_engine === 'off' ? 'volcengine' : c.transcribe_engine || 'volcengine';
  $('settingFallback').checked = c.transcribe_fallback_local !== false; $('proxyInput').value = c.proxy || '';
  for (const [id, key] of [['flagTikhub','tikhub_api_key_set'], ['flagVolc','volcengine_api_key_set'], ['flagAliyun','aliyun_api_key_set']]) $(id).textContent = c[key] ? '已配' : '未填';
  if (!state.onceOptionsDirty) { $('onceTranscribe').checked = c.transcribe_engine !== 'off'; $('onceKeep').checked = c.keep_video !== false; }
  syncTranscribeControl();
  const problems = [];
  const s = state.settingsStatus || {};
  if (!s.key_present || s.tikhub_ok === false) problems.push({ text: s.tikhub_note || 'TikHub 账号或余额有问题', action: '' });
  if (s.network?.ok === false || s.proxy?.ok === false) problems.push({ text: s.network?.message || '网络连接有问题', action: '重新检查' });
  const engine = state.config.transcribe_engine || 'volcengine';
  if (engine !== 'off') {
    const engineKey = engine === 'aliyun' ? 'aliyun' : engine === 'local' ? 'local' : 'volcengine';
    if (engineKey === 'local' && !s.local_transcribe) {
      problems.push({ text: '转写尚未配置：本机 faster-whisper 不可用', action: '' });
    } else if (engineKey === 'volcengine' && !c.volcengine_api_key_set && !s.volcengine_key) {
      problems.push({ text: '转写尚未配置：火山引擎 API Key 未填写', action: '' });
    } else if (engineKey === 'aliyun' && !c.aliyun_api_key_set && !s.aliyun_key) {
      problems.push({ text: '转写尚未配置：阿里云 API Key 未填写', action: '' });
    } else if (engineKey === 'volcengine' && s.volcengine_ok === false) {
      problems.push({ text: s.volcengine_note || '火山转写暂时不可用', action: '' });
    } else if (engineKey === 'aliyun' && s.aliyun_ok === false) {
      problems.push({ text: s.aliyun_note || '阿里云转写暂时不可用', action: '' });
    } else if (engineKey === 'volcengine' && s.volcengine_ok == null) {
      problems.push({ text: '转写尚未就绪：火山引擎状态尚未检测', action: '' });
    } else if (engineKey === 'aliyun' && s.aliyun_ok == null) {
      problems.push({ text: '转写尚未就绪：阿里云状态尚未检测', action: '' });
    }
  }
  if (!state.favoriteStatus?.configured) problems.push({ text: '收藏夹同步没配置', action: '' });
  const favMessage = String(state.favoriteStatus?.message || '');
  if (state.favoriteStatus?.login_expired || /登录.*过期|登录失效/.test(favMessage)) problems.push({ text: '收藏夹登录过期', action: '' });
  const box = $('settingsProblems'); box.replaceChildren();
    problems.forEach(p => { const row = node('div', 'settings-problem', p.text); if (p.action === '重新检查') { const b = node('button', 'problem-link', p.action); b.addEventListener('click', async () => { await api('/api/recheck', { method: 'POST', body: {} }).catch(() => {}); await loadSettings(); }); row.append(' · ', b); } box.append(row); });
  box.hidden = !problems.length; $('settingsDot').hidden = !problems.length;
}
async function saveConfig(patch) {
  try { await api('/api/config', { method: 'POST', body: patch }); const r = await api('/api/config'); state.config = r.config || {}; renderSettings(); toast('已保存'); }
  catch (err) { toast(err.message, true); }
}
async function loadCollectDefaults() {
  try {
    const r = await api('/api/config'); state.config = r.config || {};
    if (!state.onceOptionsDirty) { $('onceTranscribe').checked = state.config.transcribe_engine !== 'off'; $('onceKeep').checked = state.config.keep_video !== false; }
    syncTranscribeControl();
    state.configLoaded = true;
    if (!viralDirty) {
      const t = state.config.viral?.tier;
      if (Number.isInteger(t) && t >= 0 && t <= 4) $('viralTier').value = t;
      const days = state.config.viral?.publish_time;
      if ([0, 7, 180].includes(days)) $('viralSourceWindow').value = days;
      const sources = state.config.viral?.douyin_sources || (state.config.viral?.douyin?.source ? [state.config.viral.douyin.source] : ['search', 'lowfan']);
      $('viralSearchSource').checked = sources.includes('search');
      $('viralLowfanSource').checked = sources.includes('lowfan');
      const pages = state.config.viral?.max_pages;
      if (Number.isInteger(pages) && pages >= 1 && pages <= 3) $('viralMaxPages').value = pages;
      applyViralTier();
      for (const p of ['douyin', 'xiaohongshu']) {
        for (const k of ViralFilter.keys) {
          const v = state.config.viral?.[p]?.[k];
          if (typeof v === 'number' && Number.isFinite(v) && v >= 0) {
            $(viralInputId(p, k)).value = v;
            if (v !== ViralFilter.preset(p, Number($('viralTier').value))[k]) viralCustom = true;
          }
        }
      }
      updateViralLabel();
    }
  } catch (err) { toast(`设置没读出来：${err.message}`, true); }
  syncCount();
}
function syncTranscribeControl() {
  const disabled = state.config.transcribe_engine === 'off';
  $('onceTranscribe').disabled = disabled;
  const hint = $('onceTranscribeHint');
  if (hint) {
    hint.hidden = !disabled;
    hint.textContent = disabled ? '请先在设置中开启并配置转写服务。' : '';
  }
}

function setRoute() {
  const hash = location.hash || '#/';
  const item = hash.match(/^#\/item\/(.+)$/);
  if (item) { openDetail(decodeURIComponent(item[1])); return; }
  if (hash.startsWith('#/viral')) { showPage('viral'); return; }
  if (hash.startsWith('#/settings')) { showPage('settings'); loadSettings(); return; }
  if (hash.startsWith('#/accounts') || hash.startsWith('#/creators')) { showPage('creators'); loadCreators(); return; }
  if (hash.startsWith('#/library') || hash.startsWith('#/favorites')) { showPage('library'); state.tab = hash.startsWith('#/favorites') ? 'favorites' : 'all'; if (state.tab !== 'all') { state.filters.author = ''; state.filters.authorId = ''; state.selected.clear(); } updateLibraryTabs(); loadLibrary(); return; }
  if (hash.startsWith('#/jobs')) { showPage('collect'); renderCollectOverview(); return; }
  if (hash.startsWith('#/pick')) { showPage('collect'); if (!state.preview && state.previewUrl) beginPreview(state.previewUrl); return; }
  showPage('collect');
}
function updateLibraryTabs() { document.querySelectorAll('#libraryTabs button').forEach(b => b.classList.toggle('is-active', b.dataset.tab === state.tab)); }

function bind() {
  initViralControls();
  $('viralForm').addEventListener('submit', searchViral);
  $('viralSave').addEventListener('click', saveViral);
  $('viralAll').addEventListener('change', () => { viralRows.filter(viralSelectable).forEach(x => { if ($('viralAll').checked) viralSelected.add(x.key); else viralSelected.delete(x.key); }); renderViral(); });
  $('collectForm').addEventListener('submit', submitCollect);
  $('urlInput').addEventListener('input', () => { invalidatePreview(); state.resolveSeq = (state.resolveSeq || 0) + 1; state.preview = null; state.previewLoading = false; state.profileMode = false; state.collectResolving = false; state.collectResolving = linksFrom($('urlInput').value).length > 0; $('inlinePreview').hidden = true; $('collectLower').hidden = false; $('previewShortcuts').hidden = true; syncCount(); scheduleInspect(); });
  $('urlInput').addEventListener('paste', () => setTimeout(scheduleInspect, 0));
  $('gearButton').addEventListener('click', () => { $('collectPopover').hidden = !$('collectPopover').hidden; });
  $('onceTranscribe').addEventListener('change', () => { state.onceOptionsDirty = true; });
  $('onceKeep').addEventListener('change', () => { state.onceOptionsDirty = true; });
  $('onlyNew').addEventListener('change', () => { state.onlyNew = $('onlyNew').checked; renderPreview(); });
  $('loadMore').addEventListener('click', loadPreviewMore);
  $('refreshPreview').addEventListener('click', () => { invalidatePreview(); const seq = ++state.previewSeq; state.previewLoading = true; syncCount(); requestPreview(state.previewLimit, '', seq); });
  $('closePreview').addEventListener('click', () => { invalidatePreview(); state.preview = null; state.previewLoading = false; state.profileMode = false; $('inlinePreview').hidden = true; $('collectLower').hidden = false; $('previewShortcuts').hidden = true; syncCount(); });
  $('urlInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitCollect(); } });
  $('syncFavorites').addEventListener('click', syncFavorites); $('favoriteRetry').addEventListener('click', syncFavorites); $('shortcutFavorites').addEventListener('click', syncFavorites);
  $('addCreator').addEventListener('click', () => { $('creatorAdd').hidden = !$('creatorAdd').hidden; }); $('saveCreator').addEventListener('click', addCreator);
  $('cancelCreator').addEventListener('click', () => { $('creatorAdd').hidden = true; });
  $('libraryTabs').addEventListener('click', e => { const tab = e.target.closest('button[data-tab]'); if (!tab) return; clearSelected(); state.tab = tab.dataset.tab; if (state.tab !== 'all') { state.filters.author = ''; state.filters.authorId = ''; $('authorFilter').value = ''; } updateLibraryTabs(); loadLibrary(); });
  $('searchInput').addEventListener('input', e => { clearSelected(); renderLibrary(); clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => { state.filters.q = e.target.value.trim(); loadLibrary(); }, 250); });
  $('filterButton').addEventListener('click', () => { const wasHidden = $('filterPopover').hidden; closePopovers(); $('filterPopover').hidden = !wasHidden; });
  for (const [id, key] of [['authorFilter','author'], ['platformFilter','platform'], ['collectionFilter','collection']]) $(id).addEventListener('change', e => {
    clearSelected();
    if (key === 'author') {
      state.filters.authorId = e.target.value;
      state.filters.author = state.libraryPageMeta?.author_counts?.find(group => group.id === e.target.value)?.name || '';
    } else state.filters[key] = e.target.value;
    loadLibrary();
  });
  $('libraryMore').addEventListener('click', () => { state.offset += 200; loadLibrary(false); });
  $('copyFiltered').addEventListener('click', copyAllFiltered);
  $('multiToggle').addEventListener('click', () => setMulti(!state.multi));
  $('multiMore').addEventListener('click', () => { const wasHidden = $('multiPopover').hidden; closePopovers(); $('multiPopover').hidden = !wasHidden; });
  $('leaveMulti').addEventListener('click', () => setMulti(false)); $('deleteSelected').addEventListener('click', deleteSelected);
  $('copySelected').addEventListener('click', () => copyForAI(selectedRows()));
  $('detailBack').addEventListener('click', () => { location.hash = '#/library'; }); $('copyOne').addEventListener('click', () => copyDetail('ai'));
  $('detailMore').addEventListener('click', () => { const wasHidden = $('detailMenu').hidden; closePopovers(); $('detailMenu').hidden = !wasHidden; });
  $('detailMenu').addEventListener('click', e => { const action = e.target.closest('[data-action]')?.dataset.action; if (action) { $('detailMenu').hidden = true; copyDetail(action); } });
  $('settingTranscribe').addEventListener('change', e => saveConfig({ transcribe_engine: e.target.checked ? ($('settingEngine').value || 'volcengine') : 'off' }));
  $('settingEngine').addEventListener('change', e => { if ($('settingTranscribe').checked) saveConfig({ transcribe_engine: e.target.value }); });
  $('settingKeep').addEventListener('change', e => saveConfig({ keep_video: e.target.checked })); $('settingFallback').addEventListener('change', e => saveConfig({ transcribe_fallback_local: e.target.checked }));
  $('proxyInput').addEventListener('change', e => saveConfig({ proxy: e.target.value.trim() }));
  for (const [id, key] of [['keyTikhub','tikhub_api_key'],['keyVolc','volcengine_api_key'],['keyAliyun','aliyun_api_key']]) $(id).addEventListener('change', e => { const value = e.target.value.trim(); e.target.value = ''; if (value) saveConfig({ [key]: value }); });
  document.addEventListener('click', e => {
    const inside = e.target.closest('.popover, #gearButton, #filterButton, #multiMore, #detailMore, .creator-menu, .card-more');
    if (!inside) closePopovers();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePopovers(); });
  window.addEventListener('hashchange', setRoute);
}

// 最近一批预览保存在当前浏览器；审阅反馈保存在服务端本地 JSON。
let viralRows = [], viralSelected = new Set(), viralBusy = false;
let viralBatch = null, viralCustom = false, viralDirty = false;
let viralRBusy = false, viralRPlan = null;
let viralFeedback = {items: {}, authors: {}}, viralFeedbackBusy = false;
const VIRAL_CACHE_KEY = 'glean.viral.preview.v1';
const viralCacheControls = ['viralExclude', 'viralFilterCommerce', 'viralSort', 'viralGrouping',
  'viralShowIrrelevant', 'viralShowBlocked', 'viralShowPool', 'viralKeywords', 'viralSourceWindow',
  'viralMaxPages', 'viralSearchSource', 'viralLowfanSource', 'viralDouyin', 'viralXhs', 'viralTier'];
function cacheViral() {
  if (!viralBatch?.pool) return;
  try {
    const controls = Object.fromEntries(viralCacheControls.map(id => [id, {value: $(id).value, checked: $(id).checked}]));
    localStorage.setItem(VIRAL_CACHE_KEY, JSON.stringify({version: 1, batch: viralBatch, controls, rules: viralRules(), custom: viralCustom}));
    $('viralStorageStatus').textContent = '已保存本批预览；刷新后可继续免费筛选。反馈保存在本机 data/viral-feedback.json。';
  } catch {
    $('viralStorageStatus').textContent = '浏览器无法保存预览（空间不足或存储被禁用），刷新可能丢失本批；反馈仍单独保存。';
  }
}
function restoreViral() {
  try {
    const cached = JSON.parse(localStorage.getItem(VIRAL_CACHE_KEY) || 'null');
    if (!cached || cached.version !== 1 || !Array.isArray(cached.batch?.pool)) return;
    if (cached.batch.pool.some(row => !row || !['douyin', 'xiaohongshu'].includes(row.platform))) throw Error('缓存作品格式错误');
    if (!['douyin', 'xiaohongshu'].every(p => ViralFilter.keys.every(k =>
      Number.isFinite(Number(cached.rules?.[p]?.[k])) && Number(cached.rules[p][k]) >= 0))) throw Error('缓存门槛格式错误');
    for (const [id, value] of Object.entries(cached.controls || {})) {
      if (!viralCacheControls.includes(id)) continue;
      $(id).value = value.value; $(id).checked = value.checked;
    }
    for (const p of ['douyin', 'xiaohongshu']) for (const k of ViralFilter.keys)
      $(viralInputId(p, k)).value = cached.rules[p][k];
    viralCustom = !!cached.custom; viralDirty = true; viralBatch = cached.batch;
    updateViralLabel(); refilterViral();
  } catch { viralBatch = null; viralRows = []; $('viralStorageStatus').textContent = '最近预览无法恢复，请重新查询。'; }
}
async function loadViralFeedback() {
  try { viralFeedback = (await api('/api/viral/feedback')).feedback; refilterViral(); }
  catch (err) { $('viralStorageStatus').textContent = `读取反馈失败：${err.message}`; }
}
async function setViralFeedback(row, kind, mark) {
  if (viralFeedbackBusy) return;
  viralFeedbackBusy = true;
  try {
    viralFeedback = (await api('/api/viral/feedback', {method: 'POST', body: {
      platform: row.platform, id: kind === 'author' ? row.author_id : row.id, kind, mark
    }})).feedback;
    refilterViral();
  } catch (err) { $('viralStorageStatus').textContent = `保存反馈失败：${err.message}；本次标记未保存。`; }
  finally { viralFeedbackBusy = false; }
}
const viralSelectable = row => !row.local && row.group !== '已排除';
const viralInputId = (p, k) => `viral-${p}-${k}`;
function viralRules() {
  return Object.fromEntries(['douyin', 'xiaohongshu'].map(p => [p,
    Object.fromEntries(ViralFilter.keys.map(k => [k, Number($(viralInputId(p, k)).value)]))]));
}
function updateViralLabel() {
  const name = viralCustom ? '自定义' : ViralFilter.names[Number($('viralTier').value)];
  $('viralTierName').textContent = name;
  $('viralTier').setAttribute('aria-valuetext', name);
}
function applyViralTier() {
  const tier = Number($('viralTier').value);
  for (const p of ['douyin', 'xiaohongshu']) for (const k of ViralFilter.keys)
    $(viralInputId(p, k)).value = ViralFilter.preset(p, tier)[k];
  viralCustom = false; updateViralLabel(); refilterViral();
}
function initViralControls() {
  const labels = ['粉丝上限', '最低互动', '最低倍数', '本地天数'];
  for (const p of ['douyin', 'xiaohongshu']) {
    const group = node('fieldset'); group.append(node('legend', '', platformLabel(p)));
    ViralFilter.keys.forEach((k, i) => {
      const label = node('label', '', labels[i]); const input = node('input');
      input.id = viralInputId(p, k); input.type = 'number'; input.min = '0';
      input.step = k === 'min_multiplier' ? 'any' : '1';
      input.required = true; label.append(input); group.append(label);
      input.addEventListener('input', () => { viralDirty = true; viralCustom = true; updateViralLabel(); refilterViral(); });
    });
    $('viralThresholds').append(group);
  }
  for (const id of ['viralKeywords', 'viralSourceWindow', 'viralMaxPages', 'viralSearchSource', 'viralLowfanSource']) $(id).addEventListener('input', () => { viralDirty = true; });
  $('viralTier').addEventListener('input', () => { viralDirty = true; applyViralTier(); });
  for (const id of ['viralShowPool', 'viralFilterCommerce', 'viralSort', 'viralGrouping', 'viralShowIrrelevant', 'viralShowBlocked'])
    $(id).addEventListener('change', refilterViral);
  $('viralExclude').addEventListener('input', refilterViral);
  for (const id of ['viralRPages', 'viralRBudget']) $(id).addEventListener('input', () => {
    viralRPlan = null; $('viralRConfirm').hidden = true;
    $('viralRStatus').textContent = 'R 页数或预算已修改，请重新点击算 R 查看预计调用。';
  });
  $('viralRWorth').addEventListener('click', () => prepareViralR(null));
  $('viralRRun').addEventListener('click', runViralR);
  $('viralRCancel').addEventListener('click', () => { viralRPlan = null; $('viralRConfirm').hidden = true; $('viralRStatus').textContent = '已取消，本次未查询 TikHub。'; });
  applyViralTier();
  restoreViral();
}
function refilterViral() {
  if (!viralBatch) return;
  if (!Array.isArray(viralBatch.pool)) {
    $('viralCounts').textContent = '本批缺少参考数据，请重新查询后再调松紧。'; return;
  }
  if (!$('viralForm').checkValidity()) {
    $('viralCounts').textContent = '请填写有效的非负门槛数字。';
    viralRows = []; viralSelected.clear(); renderViral(); return;
  }
  const rules = viralRules();
  const range = viralBatch.coverage?.publish_time || 0;
  const withinRange = rule => ({...rule, max_age_days: range ? Math.min(rule.max_age_days, range) : rule.max_age_days});
  const pool = viralBatch.pool.map(row => {
    const scored = ViralFilter.score(row, withinRange(rules[row.platform]));
    scored.strict_pass = ViralFilter.score(row, withinRange(ViralFilter.preset(row.platform, 1))).passed;
    return ViralFilter.classify(scored, {exclude: $('viralExclude').value, filterCommerce: $('viralFilterCommerce').checked}, viralFeedback);
  });
  const count = pool.filter(x => x.passed && x.group === '候选' && x.mark !== '不相关').length;
  const pending = pool.filter(x => x.pending).length;
  $('viralCounts').textContent = `严选 ${pool.filter(x => x.strict_pass && x.group === '候选' && x.mark !== '不相关').length} · 本档 ${count} · 本批参考 ${pool.length} · 待核 ${pending} · 疑似带货 ${pool.filter(x => x.group === '疑似带货').length} · 已排除 ${pool.filter(x => x.group === '已排除').length} · 不相关 ${pool.filter(x => x.mark === '不相关').length}`;
  const coverage = viralBatch.coverage || {};
  const sources = Object.entries(coverage.keywords || {}).map(([p, words]) => {
    const pages = coverage.pages_by_keyword?.[p] || {};
    return `${platformLabel(p)}：${words.map(k => `${k}（${pages[k] || 0}页）`).join('、')}，拉源 ${coverage.source_calls?.[p] || 0} 次`;
  }).join('；');
  const rangeLabel = {7:'近 7 天', 180:'近半年', 0:'不限'}[coverage.publish_time ?? 7];
  const base = `${sources ? sources + '。' : ''}发布时间范围：${rangeLabel}。${coverage.douyin_sources?.includes('lowfan') ? '低粉榜固定 168 小时，每词 1 页。' : ''}每词最多 ${coverage.max_pages || 1} 页，扫描 ${viralBatch.scanned || 0} 条，可识别去重 ${pool.length} 条，TikHub 调用 ${viralBatch.calls || 0} 次（含补粉，重试另计）。同批重筛不消耗查询额度。`;
  $('viralStatus').textContent = base + (count ? '' : ' 当前档没有候选，可往宽松拖或看本批参考；不能据此判断关键词没有好内容。')
    + (viralBatch.coverage?.incomplete ? ' 覆盖不全，结果未完整核对。' : '') + (viralBatch.warnings || []).join('；');
  viralRows = ViralFilter.sortRows(pool.filter(x =>
    (!x.blocked || $('viralShowBlocked').checked) && (x.mark !== '不相关' || $('viralShowIrrelevant').checked) &&
    ($('viralShowPool').checked || x.passed || x.group !== '候选')), $('viralSort').value || 'multiplier');
  const visible = new Set(viralRows.filter(viralSelectable).map(x => x.key));
  viralSelected = new Set([...viralSelected].filter(k => visible.has(k)));
  cacheViral();
  renderViral();
}
function renderViral() {
  const grid = $('viralGrid'); grid.replaceChildren();
  const sections = ViralFilter.sections(viralRows, $('viralGrouping').value === 'platform');
  for (const section of sections) {
    grid.append(node('h2', 'viral-group-title', `${section.group}${section.platform ? ' · ' + platformLabel(section.platform) : ''}（${section.items.length}）`));
    for (const row of section.items) {
      const selected = viralSelected.has(row.key);
      const card = node('article', `preview-card${selected ? ' is-selected' : ''}${!viralSelectable(row) || viralBusy ? ' is-disabled' : ''}`);
      card.setAttribute('role', 'listitem');
      if (row.cover) {
        const img = node('img', 'preview-cover'); img.loading = 'lazy'; img.alt = '';
        img.src = coverUrl(row.cover, row.url || 'https://www.douyin.com/');
        img.addEventListener('error', () => { img.replaceWith(node('div', 'preview-cover cover-empty')); }, { once: true });
        card.append(img);
      } else card.append(node('div', 'preview-cover cover-empty'));
      const check = node('span', 'card-check', selected ? '✓' : '');
      card.append(check);
      if (row.local) card.append(node('span', 'saved-mark', '已入库'));
      const body = node('div', 'viral-card-body');
      body.append(node('div', 'viral-card-title', row.title || '（无标题）'));
      body.append(node('div', 'viral-card-meta', `${platformLabel(row.platform)} · ${row.author || '作者未知'} · ${{'douyin:search':'抖音搜索','douyin:lowfan':'抖音低粉榜','xiaohongshu:search':'小红书搜索'}[row.source] || '来源待核'}`));
      const mult = row.multiplier == null ? '倍待核' : `${Number(row.multiplier).toFixed(1)}×`;
      const age = row.age_days == null ? '日期待核' : `${Math.floor(row.age_days)}天前`;
      body.append(node('div', 'viral-card-stats', `粉 ${row.fans ?? '待核'} · 互动 ${row.interactions ?? '待核'} · M ${mult} · R ${row.r?.value == null ? '未评级' : Number(row.r.value).toFixed(2) + '×'}`));
      const level = row.passed ? (row.strict_pass ? '严选 · 本档' : '本档') : `${(row.fail_reasons || ['未达标']).slice(0, 2).join('、')}`;
      body.append(node('div', 'viral-card-meta', `${level} · ${age}`));
      if (row.review_reasons.length) body.append(node('div', 'viral-review-reason', row.review_reasons.join('、')));
      if (row.commerce.length) body.append(node('div', 'viral-review-reason', `疑似带货依据：${row.commerce.join('、')}（待人工确认）`));
      if (row.mark) body.append(node('div', 'viral-review-reason', `已标记：${row.mark}`));
      if (row.r) body.append(node('div', 'viral-card-meta', `${row.r.grade} · 样本 ${row.r.samples} · 中位数 ${row.r.median ?? '未知'} · ${row.r.distribution}。${row.r.note}。${row.r.history_note} 获取于 ${new Date(row.r.fetched_at * 1000).toLocaleString()}${row.r.cache_hit ? '（缓存）' : ''}`));
      const actions = node('div', 'viral-card-actions');
      const calc = node('button', 'quiet-link', row.r ? '重算 R（优先缓存）' : '算 R'); calc.type = 'button';
      calc.disabled = viralBusy || viralRBusy;
      calc.addEventListener('click', event => { event.stopPropagation(); prepareViralR(row); });
      actions.append(calc);
      for (const mark of ['不相关', '值得跟']) {
        const button = node('button', 'quiet-link', row.mark === mark ? `取消${mark}` : mark); button.type = 'button';
        button.addEventListener('click', event => { event.stopPropagation(); setViralFeedback(row, 'item', row.mark === mark ? '' : mark); });
        actions.append(button);
      }
      if (row.author_id) {
        const block = node('button', 'quiet-link', row.blocked ? '解除屏蔽作者' : '屏蔽作者'); block.type = 'button';
        block.addEventListener('click', event => { event.stopPropagation(); setViralFeedback(row, 'author', row.blocked ? '' : '屏蔽'); });
        actions.append(block);
      }
      const select = node('button', 'quiet-link', selected ? '取消选择' : '选择入库'); select.type = 'button';
      select.disabled = !viralSelectable(row) || viralBusy; select.setAttribute('aria-pressed', String(selected));
      select.addEventListener('click', event => { event.stopPropagation(); if (viralSelected.has(row.key)) viralSelected.delete(row.key); else viralSelected.add(row.key); renderViral(); });
      actions.append(select); body.append(actions);
      if (row.url) {
        const open = node('a', 'viral-open', '打开原页'); open.href = row.url; open.target = '_blank'; open.rel = 'noreferrer';
        open.addEventListener('click', event => event.stopPropagation());
        body.append(open);
      }
      card.append(body);
      if (viralSelectable(row) && !viralBusy) {
        card.addEventListener('click', () => {
          if (viralSelected.has(row.key)) viralSelected.delete(row.key); else viralSelected.add(row.key);
          renderViral();
        });
      }
      grid.append(card);
    }
  }
  const fresh = viralRows.filter(viralSelectable);
  $('viralAll').checked = fresh.length > 0 && fresh.every(x => viralSelected.has(x.key));
  $('viralAll').disabled = viralBusy || !fresh.length;
  $('viralSave').disabled = viralBusy || !viralSelected.size || viralSelected.size > 100;
  $('viralRWorth').disabled = viralRBusy || viralBusy;
  $('viralRRun').disabled = viralRBusy;
  $('viralSearch').disabled = viralBusy || viralRBusy;
  $('viralSearch').textContent = viralBatch ? '重新查询（消耗额度）' : '查询（消耗额度）';
  $('viralTier').disabled = viralBusy;
  $('viralThresholds').querySelectorAll('input').forEach(input => { input.disabled = viralBusy; });
  $('viralSave').textContent = viralSelected.size > 100 ? '每次最多入库100条，请减少勾选' : viralSelected.size ? `入库 ${viralSelected.size} 条` : '勾选后入库';
}
async function searchViral(event) {
  event.preventDefault(); if (viralBusy || viralRBusy) return;
  const platforms = []; if ($('viralDouyin').checked) platforms.push('douyin'); if ($('viralXhs').checked) platforms.push('xiaohongshu');
  if (!platforms.length) { $('viralStatus').textContent = '请至少选择一个平台'; return; }
  const douyin_sources = [];
  if ($('viralSearchSource').checked) douyin_sources.push('search');
  if ($('viralLowfanSource').checked) douyin_sources.push('lowfan');
  if (platforms.includes('douyin') && !douyin_sources.length) { $('viralStatus').textContent = '请至少选择一个抖音来源'; return; }
  viralDirty = true;
  const keywords = [...new Set($('viralKeywords').value.split(/[,，\r\n]/).map(x => x.trim()).filter(Boolean))];
  if (keywords.length > 20 || keywords.some(k => [...k].length > 80)) {
    $('viralStatus').textContent = '最多 20 个关键词，每个最多 80 字'; return;
  }
  viralRPlan = null; $('viralRConfirm').hidden = true; $('viralRStatus').textContent = '';
  viralBusy = true; viralBatch = null; $('viralCounts').textContent = ''; viralRows = []; viralSelected.clear(); renderViral();
  $('viralStatus').textContent = '正在查询候选，可能需要几分钟…';
  try {
    const r = await api('/api/viral/preview', { method: 'POST', body: { platforms, publish_time: Number($('viralSourceWindow').value), ...(douyin_sources.length ? {douyin_sources} : {}), max_pages: Number($('viralMaxPages').value), ...(keywords.length ? { keywords } : {}), tier: Number($('viralTier').value), thresholds: viralRules(), limit: 100 } });
    watchJob(r.job_id, job => {
      if (!['done', 'partial', 'failed'].includes(job.state)) return;
      viralBusy = false;
      if (job.state === 'failed') $('viralStatus').textContent = job.error || '查询失败';
      else {
        viralBatch = job.viral || {};
        viralRows = viralBatch.candidates || [];
        refilterViral();
      }
      renderViral();
    });
  } catch (err) { viralBusy = false; renderViral(); $('viralStatus').textContent = err.message; }
}
async function prepareViralR(row) {
  if (viralRBusy || viralBusy) return;
  viralRPlan = null; $('viralRConfirm').hidden = true;
  viralRBusy = true; renderViral();
  try {
    const plan = await api('/api/viral/r/estimate', {method: 'POST', body: {
      items: row ? [row] : viralBatch?.pool || [], worth_only: !row,
      pages: Number($('viralRPages').value || 2), budget: Number($('viralRBudget').value || 20)
    }});
    $('viralRStatus').textContent = `共 ${plan.authors} 位作者，${plan.cached_authors} 位有 24 小时内缓存；预计最多 ${plan.uncached_authors} 位未缓存作者 × ${plan.pages} 页 = ${plan.estimated_calls} 次 TikHub 调用，独立上限 ${plan.budget} 次（无自动重试）。${plan.missing.join('；')}${plan.over_budget ? ' 超出预算，请减少作者或页数。' : ''}`;
    if (!plan.over_budget && plan.authors) { viralRPlan = plan; $('viralRConfirm').hidden = false; }
  } catch (err) { $('viralRStatus').textContent = `R 估算失败：${err.message}`; }
  finally { viralRBusy = false; renderViral(); }
}
async function runViralR() {
  if (!viralRPlan || viralRBusy) return;
  const plan = viralRPlan; viralRPlan = null; viralRBusy = true;
  $('viralRConfirm').hidden = true; renderViral();
  $('viralRStatus').textContent = '正在查询近作并计算 R…';
  try {
    const result = await api('/api/viral/r', {method: 'POST', body: {plan_id: plan.plan_id}, timeout: 1250000});
    for (const row of viralBatch?.pool || []) if (result.results[row.key]) row.r = result.results[row.key];
    refilterViral();
    $('viralRStatus').textContent = `本次 TikHub 调用 ${result.calls} 次，已计算 ${Object.keys(result.results).length} 条。${Object.entries(result.errors).map(([author, error]) => `${author}：${error}`).join('；')}${result.uncomputed.length ? ' 未完成：' + result.uncomputed.join('、') : ''}${plan.missing.length ? '；' + plan.missing.join('；') : ''}`;
  } catch (err) { $('viralRStatus').textContent = `R 计算失败：${err.message}；已完成作者的历史保存在本机，可再次点击复用缓存。`; }
  finally { viralRBusy = false; renderViral(); }
}
function markViralLocal(row) {
  row.local = true; viralSelected.delete(row.key);
  const original = viralBatch?.pool?.find(x => x.key === row.key);
  if (original) original.local = true;
  cacheViral();
}
async function saveViral() {
  if (viralBusy) return;
  const selected = viralRows.filter(x => viralSelected.has(x.key) && viralSelectable(x));
  if (!selected.length || selected.length > 100) return;
  viralBusy = true; renderViral();
  try {
    const r = await api('/api/viral/save', { method: 'POST', body: { items: selected.map(x => ({ platform: x.platform, id: x.id, url: x.url })), options: { transcribe: $('viralTranscribe').checked } } });
    for (const existing of r.existing || []) { const row = viralRows.find(x => x.key === existing.id); if (row) { markViralLocal(row); } }
    if (!r.job_id) { viralBusy = false; renderViral(); $('viralStatus').textContent = '所选作品已在本地，已跳过下载'; return; }
    $('viralStatus').textContent = '正在保存所选作品…'; refreshJobs();
    watchJob(r.job_id, job => {
      $('viralStatus').textContent = job.progress?.detail || job.progress?.label || '正在保存…';
      if (!['done', 'partial', 'failed'].includes(job.state)) return;
      viralBusy = false;
      for (const child of job.partial?.children || []) {
        if (!child.local) continue;
        const row = viralRows.find(x => x.url === child.url);
        if (row) { markViralLocal(row); }
      }
      renderViral();
      $('viralStatus').textContent = job.state === 'failed' ? job.error : `完成：${job.children_ok || 0} 条成功，${job.children_failed || 0} 条失败。${(job.warnings || []).join('；')}`;
    });
  } catch (err) { viralBusy = false; renderViral(); $('viralStatus').textContent = err.message; }
}

bind();
loadViralFeedback();
showPage('collect');
syncCount();
loadCollectDefaults();
setRoute();
refreshJobs();
pollFavorites();
setInterval(refreshJobs, 5000);
