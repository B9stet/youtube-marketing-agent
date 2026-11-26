/* Live prototype for the v2 layout. All service requests use the local server. */
const $ = id => document.getElementById(id);
const state = {
  videos: [], selected: null, analysis: null, settings: null, status: null,
  busy: false, posting: false, token: 0, filter: 'all', historyFilter: 'all',
  currentAttempt: null, analysisError: '', lastPrevious: '', edits: loadEdits(), risingPolling: null,
  pending: null, jobStage: '', reviewing: new Set(), switchingDraft: false, variantPreview: null, reviewPolling: null,
};
const editSync = new Map();
const genLabels = {local: '10100 연결', openai: 'OpenAI API', gcp: 'GCP API'};
const jevLabels = {existing: '기존 Jev', official: '공식 Jev'};
const scoreLabels = {scene_error: '장면', stance_drift: '관점', emotion_flattened: '감정',
  brand_jump: '연결', advertorial: '광고투', repetition: '반복', tone_mismatch: '말투', false_brand_review: '이용 후기'};
const stages = {queued: '분석을 준비하고 있어요.', collecting: '영상 정보, 자막과 시청자 댓글을 불러오고 있어요.',
  selecting: '영상에서 참고할 구간을 고르고 있어요.', generating: '영상의 관점에 맞춰 댓글을 작성하고 있어요.',
  review_queued: 'Jev 검토를 기다리고 있어요.', correction_queued: '교정 작업을 기다리고 있어요.',
  diagnosing: 'Jev로 댓글을 살펴보고 있어요.', correcting: '검토 신호에 맞춰 댓글을 다듬고 있어요.',
  verifying: '교정된 댓글을 다시 살펴보고 있어요.'};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let toastTimer;

function loadEdits() {
  try { return new Map(JSON.parse(sessionStorage.getItem('comment-studio-edits') || '[]')); }
  catch { return new Map(); }
}
function saveEdits() {
  try { sessionStorage.setItem('comment-studio-edits', JSON.stringify([...state.edits])); }
  catch { /* The current page still retains edits in memory. */ }
}

function toast(message) {
  $('toast').textContent = message;
  $('toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.remove('show'), 4500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '요청을 처리하지 못했습니다.');
  return data;
}
async function latestAnalysisIfOlder(value, videoId) {
  if (state.selected === videoId && state.analysis && (value.analysis_revision || 0) < (state.analysis.analysis_revision || 0)) {
    return api('/api/v2/analysis/' + encodeURIComponent(videoId));
  }
  return value;
}

function selectedVideo() { return state.videos.find(video => video.id === state.selected) || null; }
function selectedScene() { return state.analysis?.scenes?.find(scene => scene.id === state.analysis.selected_scene) || null; }
function pendingScene() { return state.analysis?.scenes?.find(scene => scene.id === state.pending?.scene_id) || selectedScene(); }
function draft() { return state.analysis?.draft || null; }
function versionById(id) { return state.analysis?.draft_versions?.find(item => item.id === id) || null; }
function analysisSignature(value) {
  return JSON.stringify([value?.analysis_revision, value?.active_draft_id, value?.candidate_draft_id,
    (value?.draft_versions || []).map(item => [item.id, item.edit_revision, item.review_pending, item.review_stage,
      item.diagnosis_text, item.review?.action, !!item.correction_suggestion, item.correction_error])]);
}
function resetPending() {
  if (!state.analysis) { state.pending = null; return; }
  state.pending = {scene_id:state.analysis.selected_scene || state.analysis.scenes?.[0]?.id || null,
    perspective:state.analysis.perspective || 'reference', tone:state.analysis.tone || 'natural',
    spelling:!!state.analysis.spelling, reference_index:draft()?.requested_reference_index ?? null};
}
function editKeyFor(videoId, draftId) { return `${videoId}\u0001${draftId}`; }
function reviewingCurrent() { return state.reviewing.has(editKeyFor(state.selected || '', draft()?.id || '')); }
function editEntry(videoId, item) {
  const key = editKeyFor(videoId, item.id);
  if (!editSync.has(key)) editSync.set(key, {key, videoId, draftId:item.id, revision:item.edit_revision || 0,
    saved:item.comment || '', desired:item.comment || '', timer:null, running:null});
  return editSync.get(key);
}
function queueDraftEdit() {
  const item = draft(); if (!item?.id || !state.selected) return;
  const entry = editEntry(state.selected, item), text = $('draftInput').value;
  entry.desired = text; state.edits.set(entry.key, text); saveEdits();
  clearTimeout(entry.timer);
  entry.timer = setTimeout(() => flushEntry(entry).catch(error => toast(error.message)), 650);
}
async function flushEntry(entry) {
  clearTimeout(entry.timer); entry.timer = null;
  if (entry.running) { await entry.running; if (entry.desired !== entry.saved) return flushEntry(entry); return; }
  entry.running = (async () => {
    while (entry.desired !== entry.saved) {
      const submitted = entry.desired;
      const result = await api('/api/v2/drafts/edit', {method:'POST',body:JSON.stringify({video_id:entry.videoId,
        draft_id:entry.draftId, comment:submitted, expected_revision:entry.revision})});
      entry.saved = submitted; entry.revision = result.edit_revision;
      if (state.selected === entry.videoId) {
        if (state.analysis) state.analysis.analysis_revision = Math.max(state.analysis.analysis_revision || 0, result.analysis_revision || 0);
        const version = versionById(entry.draftId);
        if (version) { version.comment = submitted; version.edit_revision = result.edit_revision; }
        if (draft()?.id === entry.draftId) { draft().comment = submitted; draft().edit_revision = result.edit_revision; }
      }
    }
    if (state.edits.get(entry.key) === entry.saved) { state.edits.delete(entry.key); saveEdits(); }
  })();
  try { await entry.running; } finally { entry.running = null; }
}
async function flushCurrentEdit() {
  const item = draft(); if (state.selected && item?.id) await flushEntry(editEntry(state.selected, item));
}
function countChars(text) {
  return typeof Intl.Segmenter === 'function'
    ? [...new Intl.Segmenter('ko', {granularity: 'grapheme'}).segment(text)].length : [...text].length;
}
function claimsDirectBrandUse(comment) {
  const nonAssertive = /^(?:으면|다면|더라면|을까|다던데|다더라|대요|다고\s*들|을\s*(?:것|듯|텐데))/;
  for (let at = comment.indexOf('운명원'); at >= 0; at = comment.indexOf('운명원', at + 3)) {
    const rest = comment.slice(at + 3);
    const verbs = /이용했|이용해\s*봤|다녀왔|상담\s*받았|사주(?:를|도)?\s*(?:직접\s*)?(?:잘\s*)?봤|(?:에서|거기서)\s*(?:직접\s*)?봤|받아\s*봤|써\s*봤/g;
    for (const match of rest.matchAll(verbs)) {
      if (match.index > 30) break;
      if (!nonAssertive.test(rest.slice(match.index + match[0].length))) return true;
    }
  }
  return false;
}

function setView(name) {
  $('studioView').hidden = name !== 'studio';
  $('historyView').hidden = name !== 'history';
  document.querySelectorAll('.rail-btn[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === name));
  $('viewBreadcrumb').textContent = name === 'history' ? '작업 이력' : '유튜브 댓글 스튜디오';
}
document.querySelectorAll('.rail-btn[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view)));
$('openFullHistoryBtn').addEventListener('click', () => setView('history'));

function refreshSettingsLabels() {
  if (!state.settings) return;
  $('generationSummary').textContent = genLabels[state.settings.generation_provider];
  $('jevSummary').textContent = jevLabels[state.settings.jev_provider];
  renderProfile();
}

function profileLabel(profile) { return ({youtube1:'유튜브1',youtube2:'유튜브2',youtube3:'유튜브3'})[profile] || '자동 배정'; }
function renderProfile() {
  const selection = state.settings?.profile || 'youtube1';
  const next = state.status?.auto_assignments?.[state.selected] || state.status?.next_auto_profile || 'youtube1';
  const label = selection === 'auto' ? `자동 · ${profileLabel(next)}` : profileLabel(selection);
  $('profileSelect').value = selection;
  $('profileSelect').disabled = state.posting;
  $('prepareProfile').disabled = state.posting || selection === 'auto';
  $('activeProfileLabel').textContent = label;
  $('previewProfile').textContent = `Chrome · ${label}`;
  $('previewHandle').textContent = label;
  $('previewAvatar').textContent = selection === 'auto' ? '자동' : `유${selection.slice(-1)}`;
  $('railProfile').textContent = selection === 'auto' ? '자동' : `유${selection.slice(-1)}`;
  $('postLabel').textContent = `${label}로 댓글 달기`;
}

function renderVideos() {
  const list = $('videoList');
  list.replaceChildren();
  const posted = new Set((state.status?.attempts || []).filter(x => ['reserved', 'opening', 'typing', 'submitting', 'uncertain', 'confirmed'].includes(x.status)).map(x => x.video_id));
  const visible = state.videos.filter(video => state.filter === 'all' ||
    (state.filter === 'unposted' && !posted.has(video.id)) ||
    (state.filter === 'captioned' && video.caption_state === 'available'));
  $('emptyVideos').hidden = state.videos.length > 0;
  $('videoList').hidden = state.videos.length === 0;
  $('feedFilters').hidden = state.videos.length === 0;
  $('videoCount').textContent = String(state.videos.length);
  for (const video of visible) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'video-item' + (video.id === state.selected ? ' selected' : '');
    button.disabled = state.posting;
    button.setAttribute('aria-pressed', String(video.id === state.selected));
    const wrap = document.createElement('span'); wrap.className = 'v-thumb-wrap';
    const img = document.createElement('img'); img.className = 'v-thumb'; img.alt = '';
    img.src = /^https:\/\//i.test(video.thumbnail || '') ? video.thumbnail : `https://i.ytimg.com/vi/${video.id}/hqdefault.jpg`;
    wrap.append(img);
    if (posted.has(video.id)) {
      const flag = document.createElement('span'); flag.className = 'v-flag done'; flag.textContent = '기록 있음'; wrap.append(flag);
    }
    const info = document.createElement('span'); info.className = 'v-info';
    const title = document.createElement('span'); title.className = 'v-title'; title.textContent = video.title;
    const meta = document.createElement('span'); meta.className = 'v-meta';
    meta.textContent = `${video.channel || '채널 정보 없음'} · ${video.published || '날짜 확인 중'}`;
    info.append(title, meta);
    if (video.measurement) {
      const measure = video.measurement, detail = document.createElement('span'); detail.className = 'rising-meta';
      const observed = new Date(measure.end_at * 1000).toLocaleString('ko-KR', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
      detail.textContent = `${observed} 확인 · 조회 ${Number(video.views).toLocaleString()}회 · ${measure.elapsed_seconds}초 +${measure.views_added}회 · 시간당 ${Math.floor(measure.views_per_hour).toLocaleString()}회 환산`;
      info.append(detail);
    }
    button.append(wrap, info);
    button.addEventListener('click', () => chooseVideo(video.id));
    list.append(button);
  }
  if (state.videos.length && !visible.length) {
    const message = document.createElement('p'); message.className = 'sub-hint';
    message.textContent = state.filter === 'captioned' ? '분석한 영상 중 자막이 확인된 영상이 없습니다.' : '해당하는 영상이 없습니다.';
    list.append(message);
  }
}

$('loadVideos').addEventListener('click', async () => {
  $('loadVideos').disabled = true;
  try {
    const data = await api('/api/search?query=' + encodeURIComponent('연애'));
    state.videos = data.videos || [];
    $('emptyVideoTitle').textContent = '영상을 찾지 못했어요';
    $('emptyVideoDescription').textContent = '잠시 후 다시 불러오거나 영상 링크를 직접 입력해 주세요.';
    state.filter = 'all';
    document.querySelectorAll('.feed-filter').forEach(button => button.classList.toggle('active', button.dataset.feed === 'all'));
    renderVideos();
    toast(state.videos.length ? `영상 ${state.videos.length}개를 불러왔어요.` : '조건에 맞는 영상을 찾지 못했어요.');
  } catch (error) { toast(error.message); }
  finally { $('loadVideos').disabled = false; }
});

async function followRising(jobId) {
  if (state.risingPolling === jobId) return;
  state.risingPolling = jobId; $('findRising').disabled = true;
  try {
    while (state.risingPolling === jobId) {
      const job = await api('/api/v2/rising/jobs/' + encodeURIComponent(jobId));
      let message = job.message || ({queued:'영상 탐색을 준비하고 있어요.', searching:'최근 연애 영상을 찾고 있어요.',
        checking:'영상별 조회수를 확인하고 있어요.', remeasuring:'조회수 증가량을 다시 확인하고 있어요.'})[job.stage] || '영상 탐색을 진행하고 있어요.';
      if (['checking','remeasuring'].includes(job.stage) && Number.isInteger(job.total)) message += ` (${job.scanned || 0}/${job.total})`;
      $('risingStatus').textContent = message;
      if (job.stage === 'failed') throw new Error(job.error || '영상 탐색을 마치지 못했습니다.');
      if (job.stage === 'complete') {
        applyRisingResult(job.result);
        await refreshStatus(); renderSelection();
        toast(job.result.videos.length ? `뜨기 전 영상 ${job.result.videos.length}개를 찾았어요.` : '이번 탐색에서는 조건에 맞는 영상이 없어요.');
        return;
      }
      await sleep(1200);
    }
  } catch (error) { $('risingStatus').textContent = error.message; toast(error.message); }
  finally { if (state.risingPolling === jobId) state.risingPolling = null; $('findRising').disabled = false; }
}
function applyRisingResult(result, finishedAt = null) {
  const selected = selectedVideo();
  state.videos = [...(result.videos || [])]; state.filter = 'all';
  if (selected && !state.videos.some(video => video.id === selected.id)) state.videos.unshift(selected);
  document.querySelectorAll('.feed-filter').forEach(button => button.classList.toggle('active', button.dataset.feed === 'all'));
  $('emptyVideoTitle').textContent = '조건에 맞는 영상이 없어요';
  $('emptyVideoDescription').textContent = '이번에 확인한 영상에서는 조건을 만족하는 조회수 증가가 없었습니다.';
  const time = finishedAt ? new Date(finishedAt).toLocaleString('ko-KR', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}) + ' 측정 · ' : '';
  $('risingStatus').textContent = `${time}${result.searched}개 검색 · ${result.checked}개 확인 · ${(result.videos || []).length}개 발견`;
  renderVideos();
}
$('findRising').addEventListener('click', async () => {
  $('findRising').disabled = true;
  try { const job = await api('/api/v2/rising/jobs', {method:'POST'}); await followRising(job.job_id); }
  catch (error) { $('findRising').disabled = false; $('risingStatus').textContent = error.message; toast(error.message); }
});
document.querySelectorAll('.feed-filter').forEach(button => button.addEventListener('click', () => {
  state.filter = button.dataset.feed;
  document.querySelectorAll('.feed-filter').forEach(other => other.classList.toggle('active', other === button));
  renderVideos();
}));
$('toggleUrlBtn').addEventListener('click', () => {
  $('urlDrawer').hidden = !$('urlDrawer').hidden;
  $('toggleUrlBtn').setAttribute('aria-expanded', String(!$('urlDrawer').hidden));
  if (!$('urlDrawer').hidden) $('urlInput').focus();
});
$('linkForm').addEventListener('submit', async event => {
  event.preventDefault();
  const value = $('urlInput').value.trim();
  if (!value) return;
  $('linkMessage').textContent = '영상을 확인하고 있어요.';
  try {
    const video = await api('/api/video?url=' + encodeURIComponent(value));
    if (!state.videos.some(item => item.id === video.id)) state.videos.unshift(video);
    $('linkMessage').textContent = '해당 영상을 선택했습니다.';
    renderVideos();
    await chooseVideo(video.id);
  } catch (error) { $('linkMessage').textContent = error.message; }
});

async function chooseVideo(id) {
  if (state.posting) return;
  try { await flushCurrentEdit(); } catch (error) { toast(error.message); return; }
  const token = ++state.token;
  state.selected = id; state.analysis = null; state.pending = null; state.analysisError = ''; state.busy = true;
  state.jobStage = 'queued'; state.variantPreview = null;
  $('draftInput').value = ''; $('proofInput').value = '';
  renderVideos(); renderSelection();
  try {
    const active = await api('/api/v2/active/' + encodeURIComponent(id));
    if (token !== state.token) return;
    if (active.job_id) { await followJob(active.job_id, id, token); return; }
    const saved = await api('/api/v2/analysis/' + encodeURIComponent(id));
    if (token !== state.token) return;
    state.analysis = saved; resetPending();
    const video = selectedVideo(); if (video) video.caption_state = saved.caption_state;
    if (!saved.draft) { state.busy = false; await startGeneration(); return; }
    state.busy = false; setDraftFromAnalysis(); renderVideos(); renderSelection(); followReviews(id, token);
  } catch (error) {
    if (token !== state.token) return;
    state.busy = false;
    if (error.message.includes('저장된 영상 분석이 없습니다')) await startGeneration();
    else { state.analysisError = error.message; renderSelection(); toast(error.message); }
  }
}

function setDraftFromAnalysis() {
  const item = draft();
  if (!item?.id) { $('draftInput').value = ''; return; }
  const key = editKeyFor(state.selected, item.id);
  const legacyKey = [state.selected || '', state.analysis?.selected_scene || '', state.analysis?.perspective || '', item.comment || ''].join('\u0001');
  const text = state.edits.has(key) ? state.edits.get(key) : state.edits.has(legacyKey) ? state.edits.get(legacyKey) : (item.comment || '');
  $('draftInput').value = text;
  const entry = editEntry(state.selected, item);
  entry.desired = text;
  if (text !== entry.saved) { state.edits.set(key, text); queueDraftEdit(); }
  if (legacyKey !== key && state.edits.has(legacyKey)) { state.edits.delete(legacyKey); saveEdits(); }
  $('reviewAck').checked = false;
}
async function startGeneration(overrides = {}) {
  if (!state.selected || state.busy || state.posting) return;
  try { await flushCurrentEdit(); } catch (error) { toast(error.message); return; }
  const id = state.selected, token = state.token;
  if (!state.pending && state.analysis) resetPending();
  const body = {video_id: id, scene_id: state.pending?.scene_id || null,
    perspective: state.pending?.perspective || 'reference', tone: state.pending?.tone || 'natural',
    spelling: !!state.pending?.spelling, reference_index:state.pending?.reference_index ?? null,
    previous_comment: $('draftInput').value, ...overrides};
  state.busy = true; state.jobStage = 'queued'; state.analysisError = ''; $('analysisStatusText').textContent = stages.queued; renderSelection();
  try {
    const {job_id} = await api('/api/v2/jobs', {method: 'POST', body: JSON.stringify(body)});
    await followJob(job_id, id, token);
  } catch (error) {
    if (token !== state.token) return;
    state.busy = false; state.analysisError = error.message; state.jobStage = 'failed';
    renderSelection(); toast(error.message);
  }
}

async function followJob(jobId, id, token) {
  while (true) {
    const job = await api('/api/v2/jobs/' + encodeURIComponent(jobId));
    if (token !== state.token) return;
    const oldStage = state.jobStage;
    state.jobStage = job.stage;
    $('analysisStatusText').textContent = stages[job.stage] || '결과를 확인하고 있어요.';
    if (job.result && (!state.analysis || (job.result.analysis_revision || 0) >= (state.analysis.analysis_revision || 0))) {
      const priorId = draft()?.id;
      const changed = analysisSignature(job.result) !== analysisSignature(state.analysis) || oldStage !== job.stage;
      state.analysis = job.result;
      if (!state.pending) resetPending();
      if (draft()?.id && draft().id !== priorId) setDraftFromAnalysis();
      const video = selectedVideo(); if (video) video.caption_state = job.result.caption_state;
      if (changed) { renderVideos(); renderSelection(); }
    } else if (oldStage !== job.stage) {
      renderSelection();
    }
    if (job.stage === 'failed') throw new Error(job.error || '분석하지 못했습니다.');
    if (job.stage === 'complete') {
      if (job.result && state.analysis && (job.result.analysis_revision || 0) < (state.analysis.analysis_revision || 0)) {
        const fresh = await api('/api/v2/analysis/' + encodeURIComponent(id));
        if (token !== state.token) return;
        if ((fresh.analysis_revision || 0) >= (state.analysis.analysis_revision || 0)) {
          const priorId = draft()?.id; state.analysis = fresh;
          if (draft()?.id && draft().id !== priorId) setDraftFromAnalysis();
        }
      }
      state.busy = false; renderVideos(); renderSelection();
      const created = job.result?.draft_versions?.find(item => item.id === job.draft_id);
      toast(created?.action === 'hold' ? '시안을 보류했습니다. 이유를 확인해 주세요.'
        : job.result?.candidate_draft_id === job.draft_id ? '새 시안이 준비됐어요. Jev 검토는 계속 진행됩니다.' : '초안이 준비됐어요. Jev 검토는 계속 진행됩니다.');
      followReviews(id, token);
      return;
    }
    await sleep(1200);
  }
}

async function followReviews(videoId, token) {
  if (state.reviewPolling?.videoId === videoId && state.reviewPolling?.token === token) return;
  const marker = {videoId, token}; state.reviewPolling = marker;
  try {
    while (token === state.token && state.selected === videoId &&
           (state.analysis?.draft_versions || []).some(item => item.review_pending)) {
      await sleep(1200);
      const analysis = await api('/api/v2/analysis/' + encodeURIComponent(videoId));
      if (token !== state.token || state.selected !== videoId) return;
      if (!state.switchingDraft && (analysis.analysis_revision || 0) >= (state.analysis?.analysis_revision || 0)) {
        const oldId = draft()?.id;
        const changed = analysisSignature(analysis) !== analysisSignature(state.analysis);
        state.analysis = analysis;
        if (draft()?.id && draft().id !== oldId) setDraftFromAnalysis();
        if (changed) renderSelection();
      }
    }
  } catch(error) {
    if (token === state.token) { state.analysisError = error.message; renderSelection(); }
  } finally {
    if (state.reviewPolling === marker) state.reviewPolling = null;
  }
}

function showChips() {
  if (!state.pending) resetPending();
  const scenes = $('sceneTabs'); scenes.replaceChildren();
  for (const scene of state.analysis.scenes || []) {
    const button = document.createElement('button'); button.type = 'button';
    button.className = 'scene-btn' + (scene.id === state.pending.scene_id ? ' active' : '');
    button.disabled = state.posting;
    const time = document.createElement('span'); time.className = 'scene-time'; time.textContent = scene.time;
    const name = document.createElement('span'); name.className = 'scene-name'; name.textContent = scene.manual ? '직접 확인' : scene.quote || '장면 확인 필요';
    button.append(time, name);
    button.addEventListener('click', () => { if (scene.id !== state.pending.scene_id) { state.pending.scene_id = scene.id; state.pending.reference_index = null; renderSelection(); } });
    scenes.append(button);
  }
  const reactions = $('reactionChips'); reactions.replaceChildren();
  for (const item of state.analysis.perspectives || []) {
    const button = document.createElement('button'); button.type = 'button';
    button.className = 'chip' + (item.id === state.pending.perspective ? ' active' : '');
    button.textContent = item.label; button.disabled = state.posting;
    button.addEventListener('click', () => { state.pending.perspective = item.id; renderSelection(); });
    reactions.append(button);
  }
  $('reactionHint').textContent = '선택한 관점은 다음 시안을 만들 때 반영됩니다.';
  const tones = $('toneChips'); tones.replaceChildren();
  for (const [value, label] of [['natural', '원래 말투'], ['casual', '편하게']]) {
    const button = document.createElement('button'); button.type = 'button';
    button.className = 'seg-btn' + (state.pending.tone === value ? ' active' : '');
    button.textContent = label; button.disabled = state.posting;
    button.addEventListener('click', () => { state.pending.tone = value; renderSelection(); });
    tones.append(button);
  }
  $('spellingToggle').checked = !!state.pending.spelling;
  $('spellingToggle').disabled = state.posting;
}

function renderSelection() {
  const video = selectedVideo();
  const item = draft();
  const hasDraft = !!(video && state.analysis && item);
  $('emptySelection').hidden = !!video;
  $('analysisLoading').hidden = !video || !state.busy || hasDraft;
  $('selectionFailure').hidden = !video || state.busy || hasDraft || !state.analysisError;
  $('selectionFailureMessage').textContent = state.analysisError;
  $('selectionContent').hidden = !hasDraft;
  if (!hasDraft) { updateControls(); return; }
  const scene = selectedScene();
  $('selectedThumb').src = /^https:\/\//i.test(video.thumbnail || '') ? video.thumbnail : `https://i.ytimg.com/vi/${video.id}/hqdefault.jpg`;
  $('selectedTitle').textContent = video.title;
  $('selectedChannelBadge').textContent = video.channel || '채널';
  $('selectedMeta').textContent = `${video.published || '날짜 확인 중'} · ${scene?.time || '장면 확인'}`;
  const source = scene?.manual ? '직접 확인' : state.analysis.caption_state === 'available'
    ? (scene?.source === 'manual' ? '수동 자막' : '자동 자막') : state.analysis.caption_state === 'error' ? '자막 수집 오류' : '자막 없음';
  $('selectedSourceBadge').textContent = source;
  $('selectedSourceBadge').className = 'pill' + (state.analysis.caption_state !== 'available' ? ' warn' : '');
  $('watchLink').href = `https://www.youtube.com/watch?v=${video.id}${scene?.start ? '&t=' + scene.start + 's' : ''}`;
  $('worryText').textContent = item?.concern || '영상 속 고민과 댓글을 준비하고 있어요.';
  $('missingSource').hidden = state.analysis.caption_state === 'available';
  $('captionWarningTitle').textContent = state.analysis.caption_state === 'error' ? '자막을 가져오지 못했습니다' : '자막이 없어 제목과 설명만 참고했습니다';
  $('draftModel').textContent = item?.model ? ` · ${item.model}` : '';
  $('draftInput').disabled = state.posting || state.switchingDraft;
  $('nextDraft').disabled = state.busy || state.posting;
  $('generationStatus').hidden = !state.busy && !state.analysisError && !item.review_pending;
  $('generationStatusText').textContent = state.analysisError || (state.busy ? stages[state.jobStage] : stages[item.review_stage]) || 'Jev로 초안을 검토하고 있어요.';
  const currentPerspective = state.analysis.perspectives?.find(p => p.id === item.perspective)?.label || '참고 댓글 관점';
  $('currentDraftContext').textContent = `${scene?.time || '장면 확인'} · ${currentPerspective} · ${item.tone === 'casual' ? '편하게' : '원래 말투'}`;
  const select = $('draftVersionSelect'); select.replaceChildren();
  for (const [index, version] of (state.analysis.draft_versions || []).entries()) {
    const option = document.createElement('option'); option.value = version.id;
    const versionScene = state.analysis.scenes.find(s => s.id === version.scene_id);
    option.textContent = `시안 ${index + 1} · ${versionScene?.time || '장면 확인'}${version.corrected ? ' · 교정안' : ''}`;
    option.selected = version.id === item.id; select.append(option);
  }
  select.disabled = state.posting || state.switchingDraft;
  $('captionSource').textContent = source;
  showChips();
  const nextScene = pendingScene();
  $('quoteText').textContent = nextScene?.quote || '영상에서 구체적인 장면을 직접 확인해 주세요.';
  $('quoteReason').textContent = nextScene?.time || '';
  const nextPerspective = state.analysis.perspectives?.find(p => p.id === state.pending.perspective)?.label || '참고 댓글 관점';
  const changed = state.pending.scene_id !== item.scene_id || state.pending.perspective !== item.perspective ||
    state.pending.tone !== (item.tone || 'natural') || state.pending.spelling !== !!item.spelling ||
    state.pending.reference_index !== (item.requested_reference_index ?? null);
  $('pendingSettingsSummary').textContent = `다음 시안: ${nextScene?.time || '장면 확인'} · ${nextPerspective} · ${state.pending.tone === 'casual' ? '편하게' : '원래 말투'}${state.pending.spelling ? ' · 구어식 표기' : ''}${Number.isInteger(state.pending.reference_index) ? ` · 참고 댓글 ${state.pending.reference_index + 1}` : ''}${changed ? ' · 설정 변경됨' : ''}`;
  const comments = $('viewerComments'); comments.replaceChildren();
  for (const [i, comment] of (nextScene?.comments || []).entries()) {
    const row = document.createElement('button'); row.type = 'button'; row.className = 'vc-comment';
    row.disabled = state.posting;
    row.setAttribute('aria-pressed', String(state.pending.reference_index === i));
    row.title = '다음 시안에서 참고할 댓글 선택';
    const n = document.createElement('span'); n.className = 'vc-comment-idx'; n.textContent = String(i + 1);
    const text = document.createElement('span'); text.textContent = comment.text; row.append(n, text);
    row.addEventListener('click', () => { state.pending.reference_index = state.pending.reference_index === i ? null : i; renderSelection(); }); comments.append(row);
  }
  if (!comments.children.length) { const hint = document.createElement('p'); hint.className = 'sub-hint'; hint.textContent = '참고할 시청자 댓글을 가져오지 못했어요.'; comments.append(hint); }
  $('referenceView').hidden = !item && !(scene?.comments || []).length && !state.analysisError;
  const reference = Number.isInteger(item?.reference_index) ? scene?.comments?.[item.reference_index] : null;
  $('referenceKind').textContent = reference ? ` · ${reference.kind}` : '';
  $('referenceSource').hidden = !reference?.url;
  if (reference?.url) $('referenceSource').href = reference.url;
  $('referenceQuote').textContent = reference?.text || ((scene?.comments || []).length
    ? '선택된 참고 댓글이 없어요. 아래 후보에서 관련된 반응을 직접 고를 수 있습니다.'
    : '참고 댓글이 없어 영상 자료를 우선 사용했어요.');
  $('referenceReason').textContent = item?.reference_viewpoint || item?.case?.stance || '선택한 장면의 반응을 확인해 주세요.';
  $('referenceExperience').hidden = state.analysis.perspective !== 'experience';
  $('referenceExperience').textContent = '가상 시청자의 경험을 떠올리는 관점이에요.';
  const scores = item?.diagnosis?.scores;
  $('referenceJev').hidden = !scores;
  if (scores) {
    const signals = Object.entries(scores).filter(([, value]) => value >= .35).sort((a,b) => b[1] - a[1]).slice(0, 3)
      .map(([name,value]) => `${scoreLabels[name]} ${value.toFixed(2)}`);
    $('referenceJev').textContent = `Jev 검토 신호: ${signals.join(' / ') || '기준 이상 신호 없음'}`;
  }
  const referenceNote = Number.isInteger(item?.requested_reference_index) && item?.reference_index === null
    ? '선택한 댓글은 이번 시안에서 참고 대상으로 확인되지 않았어요. 장면과의 관련성을 살펴봐 주세요.' : '';
  const reviewMessage = item?.corrected && item?.review?.action === 'rewrite'
    ? '교정 후에도 높은 검토 신호가 남아 있어 직접 확인해 주세요.' : (item?.review?.reason || '');
  const message = [state.analysisError, referenceNote, item?.reason || reviewMessage, item?.correction_error].filter(Boolean).join(' ');
  $('referenceReview').hidden = !message; $('referenceReview').textContent = message;
  $('reviewDraft').disabled = !item?.case || !!item?.review_pending || reviewingCurrent() || state.posting;
  const previous = (state.analysis.previous_drafts || []).at(-1) || '';
  const beforeCorrection = item?.corrected ? (item.original || '') : '';
  const prior = previous || beforeCorrection;
  state.lastPrevious = prior;
  $('originalCompareCard').hidden = !prior && !beforeCorrection;
  $('originalCompareText').textContent = beforeCorrection || prior;
  $('originalPreviousHydrogen').textContent = beforeCorrection && previous && previous !== beforeCorrection ? previous : '';
  $('previousCaption').hidden = !$('originalPreviousHydrogen').textContent;
  $('originalToneText').textContent = '';
  $('originalVersionText').textContent = item?.corrected ? '첫 번째는 Jev 교정 전 시안이에요.' : '이전에 생성하거나 편집한 시안이에요.';
  $('originalReviewText').hidden = true;
  const candidate = versionById(state.analysis.candidate_draft_id);
  $('candidateCard').hidden = !candidate || candidate.id === item.id;
  if (candidate && candidate.id !== item.id) {
    const candidateScene = state.analysis.scenes.find(s => s.id === candidate.scene_id);
    const candidatePerspective = state.analysis.perspectives?.find(p => p.id === candidate.perspective)?.label || '참고 댓글 관점';
    $('candidateContext').textContent = `${candidateScene?.time || '장면 확인'} · ${candidatePerspective} · ${candidate.tone === 'casual' ? '편하게' : '원래 말투'}`;
    $('candidateText').textContent = candidate.comment || candidate.reason || '시안을 만들지 못했습니다.';
    $('candidateReviewState').textContent = candidate.review_pending ? (stages[candidate.review_stage] || 'Jev 검토가 진행 중입니다.') : (candidate.review?.reason || '시안을 확인하고 선택해 주세요.');
    $('applyCandidate').disabled = candidate.action !== 'draft' || !candidate.comment || state.posting;
  }
  const proposal = item.correction_suggestion;
  $('correctionCard').hidden = !proposal;
  if (proposal) $('correctionText').textContent = proposal.comment;
  $('variantCard').hidden = !state.variantPreview || state.variantPreview.draftId !== item.id;
  if (!$('variantCard').hidden) $('variantText').textContent = state.variantPreview.comment;
  updateControls();
}

function updateControls() {
  const text = $('draftInput').value.trim(), item = draft(), scene = selectedScene();
  const len = countChars(text);
  const parts = text.split(/(?<=[.!?。！？])\s+/).filter(Boolean);
  const tail = parts.slice(-2).join(' '), early = parts.slice(0, -2).join(' ');
  const checks = [
    [len >= 40 && len <= 120, '40~120자', '길이 조정'],
    [!text.startsWith('운명원') && tail.includes('운명원') && tail.includes('사주') && !early.includes('운명원'), '운명원 문구', '끝부분 문구 확인'],
    [!/(https?:\/\/|www\.|#|\b[a-z0-9-]+\.(?:com|net|org|kr|co)\b)/i.test(text), '금지 항목 없음', '링크/태그 제거'],
    [!claimsDirectBrandUse(text), '이용 후기 확인', '직접 이용 후기 확인'],
  ];
  $('draftChecks').replaceChildren();
  if (item) for (const [ok, yes, no] of checks) {
    const badge = document.createElement('span'); badge.className = 'chk' + (ok ? '' : ' bad'); badge.textContent = ok ? `✓ ${yes}` : `! ${no}`; $('draftChecks').append(badge);
  }
  $('charCounter').textContent = `${len} / 120자`;
  $('charCounterWrap').classList.toggle('invalid', len < 40 || len > 120);
  $('charBarFill').style.width = `${Math.min(100, len / 120 * 100)}%`;
  $('livePreviewText').textContent = text || '영상을 선택하면 댓글 미리보기가 표시됩니다.';
  $('livePreviewText').classList.toggle('placeholder', !text);
  const stale = !!item && (!item.diagnosis_text || text !== item.diagnosis_text.trim());
  if (item?.diagnosis?.scores) {
    const signals = Object.entries(item.diagnosis.scores).filter(([,value]) => value >= .35).sort((a,b) => b[1]-a[1]).slice(0,3)
      .map(([name,value]) => `${scoreLabels[name]} ${value.toFixed(2)}`);
    $('referenceJev').textContent = stale ? '이전 댓글의 Jev 결과입니다. 수정한 댓글은 재진단 전이에요.'
      : `Jev 검토 신호: ${signals.join(' / ') || '기준 이상 신호 없음'}`;
  }
  const review = item?.review?.action;
  const needsReview = stale || !item?.mentions_evidence || ['review_pending', 'review_facts', 'review_constraints', 'review_unavailable', 'review_signal', 'rewrite', 'hold'].includes(review);
  $('requestCorrection').hidden = review !== 'rewrite' || (!!item?.correction_suggestion && item.correction_suggestion.source_text === $('draftInput').value);
  $('requestCorrection').disabled = stale || !!item?.review_pending || reviewingCurrent() || state.posting;
  if (item?.correction_suggestion) {
    const canApply = $('draftInput').value === item.correction_suggestion.source_text;
    $('applyCorrection').disabled = !canApply || state.posting;
    $('correctionHint').textContent = canApply ? (item.correction_suggestion.review?.reason || '원문을 유지하거나 교정안을 선택할 수 있어요.')
      : '초안이 교정 기준보다 새로 수정됐어요. 현재 댓글을 다시 진단해 주세요.';
  }
  if (state.variantPreview && state.variantPreview.draftId === item?.id) {
    const canApply = $('draftInput').value === state.variantPreview.source;
    $('applyVariant').disabled = !canApply || state.posting;
    $('variantHint').textContent = canApply ? '현재 초안에 표기만 적용합니다. 적용 후 Jev 진단은 다시 확인해 주세요.' : '미리보기 이후 초안이 바뀌었어요. 다시 미리봐 주세요.';
  }
  $('reviewAckWrap').hidden = !item || !needsReview || state.busy || state.posting;
  const duplicate = (state.status?.attempts || []).some(x => x.video_id === state.selected && ['reserved','opening','typing','submitting','uncertain','confirmed'].includes(x.status));
  const full = (state.status?.count || 0) >= (state.status?.limit || 10);
  const valid = !!item && item.action === 'draft' && !!scene?.quote && checks.every(x => x[0]) && !duplicate && !full && !item.review_pending && !state.busy && !state.posting && (!needsReview || $('reviewAck').checked);
  $('postButton').disabled = !valid;
  renderProfile();
  $('ytSubmitChip').classList.toggle('ready', valid);
  $('postHint').textContent = state.posting ? '크롬에서 게시와 확인을 진행하고 있습니다.'
    : !state.selected ? '영상을 선택하고 댓글 초안을 확인해 주세요.'
    : state.busy ? '댓글을 준비하고 있습니다.'
    : item?.review_pending ? '현재 시안의 Jev 검토가 진행 중입니다.'
    : duplicate ? '이미 게시했거나 게시 확인이 필요한 영상입니다.'
    : full ? '오늘의 게시 한도에 도달했습니다.'
    : !scene?.quote ? '영상 속 장면을 직접 확인해 주세요.'
    : !item || item.action !== 'draft' ? '게시할 수 있는 댓글 시안이 필요합니다.'
    : !checks.every(x => x[0]) ? '댓글 조건을 먼저 확인해 주세요.'
    : needsReview && !$('reviewAck').checked ? '영상 장면과 최종 댓글을 직접 확인해 주세요.'
    : '전용 크롬에서 한 글자씩 입력하고 게시 확인 후 삭제합니다.';
}

$('draftInput').addEventListener('input', () => {
  queueDraftEdit(); $('reviewAck').checked = false; updateControls();
});
$('reviewAck').addEventListener('change', updateControls);
$('nextDraft').addEventListener('click', () => startGeneration());
$('retryAnalysis').addEventListener('click', () => startGeneration());
$('spellingToggle').addEventListener('change', event => { if (state.pending) { state.pending.spelling = event.target.checked; renderSelection(); } });
$('restorePrevious').addEventListener('click', () => {
  if (!state.lastPrevious) return;
  $('draftInput').value = state.lastPrevious; queueDraftEdit(); $('reviewAck').checked = false; updateControls();
  toast('이전 시안으로 바꿨어요. 수정한 댓글은 다시 확인해 주세요.');
});
$('saveProof').addEventListener('click', async () => {
  if (!state.selected || state.posting) return;
  const quote = $('proofInput').value.trim();
  if (quote.length < 4) { toast('확인한 장면을 네 글자 이상 적어 주세요.'); return; }
  const token = state.token, videoId = state.selected;
  try {
    let analysis = await api('/api/v2/evidence', {method:'POST', body:JSON.stringify({video_id:videoId, quote})});
    if (token !== state.token) return;
    const addedScene = analysis.added_scene_id;
    analysis = await latestAnalysisIfOlder(analysis, videoId);
    if (token !== state.token) return;
    state.analysis = analysis;
    if (!state.pending) resetPending();
    state.pending.scene_id = addedScene; state.pending.reference_index = null;
    renderSelection(); toast('장면을 추가했어요. 시안 생성 시 반영됩니다.');
  } catch (error) { toast(error.message); }
});
$('reviewDraft').addEventListener('click', async () => {
  if (!state.selected || reviewingCurrent() || draft()?.review_pending) return;
  try { await flushCurrentEdit(); } catch(error) { toast(error.message); return; }
  const token = state.token, videoId = state.selected, draftId = draft()?.id, comment = $('draftInput').value;
  const reviewKey = editKeyFor(videoId, draftId);
  state.reviewing.add(reviewKey); $('reviewDraft').disabled = true; $('referenceReview').hidden = false; $('referenceReview').textContent = '현재 댓글을 다시 진단하고 있어요.';
  try {
    let analysis = await api('/api/v2/review', {method:'POST', body:JSON.stringify({video_id:videoId, draft_id:draftId, comment})});
    if (token !== state.token || draft()?.id !== draftId) return;
    analysis = await latestAnalysisIfOlder(analysis, videoId);
    if (token !== state.token || draft()?.id !== draftId) return;
    state.analysis = analysis;
    const entry = editEntry(videoId, draft());
    if (entry.desired === comment) { entry.saved = draft().comment; entry.revision = draft().edit_revision || 0; }
    renderSelection(); toast('진단 결과를 반영했어요.');
  } catch (error) { $('referenceReview').textContent = error.message; toast(error.message); }
  finally { state.reviewing.delete(reviewKey); renderSelection(); }
});
$('requestCorrection').addEventListener('click', async () => {
  if (!draft()?.id || draft()?.review_pending || state.switchingDraft) return;
  try { await flushCurrentEdit(); } catch(error) { toast(error.message); return; }
  const token = state.token, videoId = state.selected, draftId = draft().id;
  try {
    const entry = editEntry(videoId, draft());
    let analysis = await api('/api/v2/drafts/correct', {method:'POST',body:JSON.stringify({video_id:videoId,draft_id:draftId,expected_revision:entry.revision})});
    if (token !== state.token || draft()?.id !== draftId) return;
    analysis = await latestAnalysisIfOlder(analysis, videoId);
    if (token !== state.token || draft()?.id !== draftId) return;
    state.analysis = analysis; renderSelection(); followReviews(videoId, token);
    toast('현재 댓글의 교정안을 만들고 있어요.');
  } catch(error) { toast(error.message); }
});

async function selectVersion(draftId) {
  if (!draftId || draftId === draft()?.id || state.switchingDraft || state.posting) return;
  state.switchingDraft = true; renderSelection();
  const token = state.token, videoId = state.selected, previousId = draft()?.id;
  try {
    await flushCurrentEdit();
    let analysis = await api('/api/v2/drafts/select', {method:'POST',body:JSON.stringify({video_id:videoId,draft_id:draftId,expected_active_id:previousId})});
    if (token !== state.token) return;
    analysis = await latestAnalysisIfOlder(analysis, videoId);
    if (token !== state.token) return;
    state.analysis = analysis; state.variantPreview = null; setDraftFromAnalysis(); renderSelection();
    toast('선택한 시안을 불러왔어요.');
  } catch(error) { toast(error.message); }
  finally { state.switchingDraft = false; renderSelection(); }
}
$('draftVersionSelect').addEventListener('change', event => selectVersion(event.target.value));
$('applyCandidate').addEventListener('click', () => selectVersion(state.analysis?.candidate_draft_id));
$('applyCorrection').addEventListener('click', async () => {
  if (!draft()?.correction_suggestion || state.switchingDraft) return;
  state.switchingDraft = true; renderSelection();
  const token = state.token, videoId = state.selected, draftId = draft().id;
  try {
    await flushCurrentEdit();
    const entry = editEntry(videoId, draft());
    let analysis = await api('/api/v2/drafts/correction', {method:'POST',body:JSON.stringify({video_id:videoId,draft_id:draftId,expected_revision:entry.revision})});
    if (token !== state.token) return;
    analysis = await latestAnalysisIfOlder(analysis, videoId);
    if (token !== state.token) return;
    state.analysis = analysis; state.variantPreview = null; setDraftFromAnalysis(); renderSelection(); toast('교정안을 새 시안으로 저장했어요.');
  } catch(error) { toast(error.message); }
  finally { state.switchingDraft = false; renderSelection(); }
});
$('previewVariant').addEventListener('click', async () => {
  if (!state.selected || !draft()?.id) return;
  const token = state.token, draftId = draft().id, source = $('draftInput').value;
  try {
    const result = await api('/api/v2/drafts/variant', {method:'POST',body:JSON.stringify({video_id:state.selected,draft_id:draftId,comment:source})});
    if (token !== state.token || draft()?.id !== draftId) return;
    if (!result.applied) { state.variantPreview = null; renderSelection(); toast('이 초안에서 적용할 표기 변형을 찾지 못했어요.'); return; }
    state.variantPreview = {draftId, source, comment:result.comment}; renderSelection();
  } catch(error) { toast(error.message); }
});
$('applyVariant').addEventListener('click', () => {
  const preview = state.variantPreview;
  if (!preview || preview.draftId !== draft()?.id || $('draftInput').value !== preview.source) return;
  $('draftInput').value = preview.comment; state.variantPreview = null; queueDraftEdit(); $('reviewAck').checked = false; renderSelection();
});

function statusText(item) {
  if (item.status === 'confirmed') return item.deletion_status === 'deleted' ? '게시 확인, 삭제 완료'
    : item.deletion_status === 'failed' ? '게시 확인, 삭제 실패' : '게시 확인, 삭제 진행 중';
  return ({reserved:'게시 예약', opening:'프로필 여는 중', typing:'입력 중', submitting:'게시 확인 중', failed:'실패', uncertain:'게시 여부 확인 필요'})[item.status] || item.status;
}
function showStage(item) {
  const mapping = {reserved:0, opening:0, typing:1, submitting:2, confirmed:4};
  const stage = mapping[item?.status] ?? -1;
  $('stageCaption').textContent = item ? statusText(item) : '대기 중';
  $('stageCaption').className = 'pill ' + (item?.status === 'failed' || item?.status === 'uncertain' || item?.deletion_status === 'failed' ? 'warn' : 'neutral');
  document.querySelectorAll('#statusList .step-row').forEach((row, i) => {
    row.classList.toggle('done', stage > i); row.classList.toggle('active', stage === i); row.classList.toggle('failed', item?.status === 'failed' && stage === i);
  });
  $('statusNote').textContent = item?.error || item?.deletion_error || (item ? statusText(item) : '게시 버튼을 누르면 전용 크롬 프로필에서 순차 입력을 시작합니다.');
  $('showLatest').hidden = !item || !(item.screenshot || (state.status?.events || []).some(x => x.attempt_id === item.id && x.screenshot));
}
function renderHistory() {
  const attempts = state.status?.attempts || [];
  $('postingCount').textContent = `${state.status?.count || 0} / ${state.status?.limit || 10}건`;
  $('railQuotaText').textContent = `${state.status?.count || 0}/${state.status?.limit || 10}`;
  $('navHistoryBadge').textContent = String(attempts.length); $('navHistoryBadge').dataset.zero = String(!attempts.length);
  $('miniHistoryWrap').hidden = !attempts.length; $('miniHistoryList').replaceChildren();
  for (const item of attempts.slice(0, 3)) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'mini-h-item';
    const top = document.createElement('span'); top.className = 'mini-h-top'; top.textContent = `${statusText(item)} · ${item.created_at || ''}`;
    const name = document.createElement('span'); name.className = 'mini-h-title'; name.textContent = state.videos.find(x => x.id === item.video_id)?.title || item.video_id;
    button.append(top, name); button.addEventListener('click', () => openResult(item)); $('miniHistoryList').append(button);
  }
  const body = $('historyBody'); body.replaceChildren();
  const filtered = attempts.filter(item => state.historyFilter === 'all' ||
    (state.historyFilter === 'success' && item.status === 'confirmed') ||
    (state.historyFilter === 'failure' && ['failed','uncertain'].includes(item.status)));
  if (!filtered.length) { const row=document.createElement('tr'), cell=document.createElement('td'); cell.colSpan=5; cell.className='history-empty'; cell.textContent='표시할 작업 기록이 없습니다.'; row.append(cell); body.append(row); }
  for (const item of filtered) {
    const row = document.createElement('tr');
    const values = [item.created_at, state.videos.find(x => x.id === item.video_id)?.title || item.video_id, item.profile, item.comment];
    values.forEach(value => { const cell=document.createElement('td'); cell.textContent=value || ''; row.append(cell); });
    const result=document.createElement('td'), button=document.createElement('button'); button.type='button'; button.className='h-link'; button.textContent=statusText(item) + ' 보기 →'; button.addEventListener('click', () => openResult(item)); result.append(button); row.append(result); body.append(row);
  }
  const current = attempts.find(item => item.id === state.currentAttempt);
  showStage(current); updateControls();
}
async function refreshStatus() {
  state.status = await api('/api/v2/status'); renderHistory(); renderVideos(); renderProfile();
}
$('profileSelect').addEventListener('change', async event => {
  const previous = state.settings.profile, selected = event.target.value;
  event.target.disabled = true;
  try { state.settings = await api('/api/v2/settings', {method:'PUT',body:JSON.stringify({...state.settings,profile:selected})}); renderProfile(); }
  catch(error) { state.settings.profile = previous; renderProfile(); toast(error.message); }
  finally { event.target.disabled = false; }
});
$('prepareProfile').addEventListener('click', async () => {
  const profile = state.settings?.profile;
  if (!profile || profile === 'auto') return;
  try { const result = await api(`/api/v2/profiles/${profile}/open`, {method:'POST'}); toast(result.message); }
  catch(error) { toast(error.message); }
});
document.querySelectorAll('#historyView .seg-btn').forEach(button => button.addEventListener('click', () => {
  state.historyFilter=button.dataset.filter;
  document.querySelectorAll('#historyView .seg-btn').forEach(other=>other.classList.toggle('active',other===button)); renderHistory();
}));
function openResult(item) {
  $('resultTitle').textContent = statusText(item);
  $('resultSubtitle').textContent = '저장된 작업 결과와 확인 화면입니다.';
  $('resultUrl').textContent = `youtube.com/watch?v=${item.video_id}`;
  $('resultVideo').textContent = state.videos.find(x => x.id === item.video_id)?.title || item.video_id;
  $('resultCommentBlock').hidden = false; $('resultText').textContent = item.comment;
  $('resultError').hidden = !item.error && !item.deletion_error; $('resultError').textContent = item.error || item.deletion_error || '';
  $('resultCaption').textContent = '게시 확인 후 삭제 상태와 실패 이유를 기록합니다.';
  let image = $('resultCapture');
  if (!image) { image=document.createElement('img'); image.id='resultCapture'; image.alt='작업 중 저장한 화면'; image.style.cssText='display:block;max-width:100%;margin-top:14px;border-radius:8px'; $('resultCommentBlock').after(image); }
  image.hidden = false; image.onerror = () => { image.hidden = true; }; image.src = `/api/v2/capture/${item.id}?t=${Date.now()}`;
  let retry=$('retryDelete');
  if (!retry) { retry=document.createElement('button'); retry.id='retryDelete'; retry.className='ghost-btn'; retry.type='button'; $('resultCaption').after(retry); }
  retry.hidden = item.status !== 'confirmed' || item.deletion_status !== 'failed';
  retry.textContent = '삭제 다시 확인'; retry.onclick = async () => {
    retry.disabled=true;
    try { await api(`/api/retry-deletion/${item.id}`,{method:'POST'}); await refreshStatus(); $('resultDialog').close(); toast('삭제 확인을 다시 진행하고 있어요.'); }
    catch(error) { toast(error.message); } finally { retry.disabled=false; }
  };
  $('resultDialog').showModal();
}
$('showLatest').addEventListener('click', () => {
  const item=(state.status?.attempts || []).find(x=>x.id===state.currentAttempt); if(item) openResult(item);
});
$('postButton').addEventListener('click', async () => {
  if ($('postButton').disabled) return;
  state.posting=true; updateControls();
  try {
    await flushCurrentEdit();
    const entry = editEntry(state.selected, draft());
    const data=await api('/api/v2/post',{method:'POST',body:JSON.stringify({video_id:state.selected,draft_id:draft().id,expected_revision:entry.revision,comment:$('draftInput').value,review_acknowledged:$('reviewAck').checked,profile:state.settings.profile})});
    state.currentAttempt=data.attempt_id;
    while (true) {
      await refreshStatus();
      const item=state.status.attempts.find(x=>x.id===data.attempt_id);
      if (!item) throw new Error('게시 작업 기록을 확인하지 못했습니다.');
      if (['failed','uncertain'].includes(item.status) || (item.status==='confirmed' && ['deleted','failed'].includes(item.deletion_status))) {
        toast(statusText(item)); break;
      }
      await sleep(1500);
    }
  } catch(error) { toast(error.message); }
  finally { state.posting=false; await refreshStatus().catch(()=>{}); updateControls(); }
});

let editingModels = {}, editingJevModels = {};
function settingDetails() {
  const provider=$('generationSelect').value;
  $('generationDetailLabel').textContent=provider==='local'?'엔드포인트':'인증 정보';
  $('generationDetail').value=provider==='local'?'http://127.0.0.1:10100/v1':provider==='openai'?'입력한 키 또는 서버 환경 변수 사용':'Google Cloud 기본 인증 사용';
  $('generationModel').value=editingModels[provider] || '';
  $('generationModel').placeholder=provider==='local'?'모델명':'사용할 모델명 입력';
  $('gcpProjectField').hidden=provider!=='gcp'; $('gcpRegionField').hidden=provider!=='gcp';
  $('openaiKeyField').hidden=provider!=='openai';
}
function settingJev() { $('jevModel').value=editingJevModels[$('jevSelect').value] || ''; }
function credentialLabel(value) {
  return ({session:'현재 실행 중인 앱에 키가 입력되어 있어요.',environment:'서버에 설정된 키를 사용합니다.',missing:'입력한 키는 앱을 실행하는 동안만 사용합니다.'})[value] || '';
}
async function openSettings() {
  if (!state.settings) { toast('연결 설정을 불러오고 있어요.'); return; }
  editingModels={local:'google-vertex/gemini-4-hydrogen',openai:'',gcp:'',...state.settings.generation_models};
  editingModels[state.settings.generation_provider]=state.settings.generation_model;
  editingJevModels={existing:'jev-latest:free',official:'jev-latest',...state.settings.jev_models};
  editingJevModels[state.settings.jev_provider]=state.settings.jev_model;
  $('generationSelect').value=state.settings.generation_provider; $('jevSelect').value=state.settings.jev_provider;
  $('gcpProject').value=state.settings.gcp_project; $('gcpRegion').value=state.settings.gcp_region;
  $('openaiKey').value='';
  try { const status = await api('/api/v2/credentials'); $('openaiKeyStatus').textContent=credentialLabel(status.openai); }
  catch { $('openaiKeyStatus').textContent='입력한 키는 앱을 실행하는 동안만 사용합니다.'; }
  settingDetails(); settingJev(); $('settingsDialog').showModal();
}
$('openSettings').addEventListener('click',openSettings); $('railSettingsBtn').addEventListener('click',openSettings);
$('generationSelect').addEventListener('change',settingDetails); $('jevSelect').addEventListener('change',settingJev);
$('generationModel').addEventListener('input',()=>{editingModels[$('generationSelect').value]=$('generationModel').value;});
$('jevModel').addEventListener('input',()=>{editingJevModels[$('jevSelect').value]=$('jevModel').value;});
$('saveSettings').addEventListener('click',async()=>{
  const value={profile:state.settings.profile,generation_provider:$('generationSelect').value,generation_model:$('generationModel').value.trim(),
    jev_provider:$('jevSelect').value,jev_model:$('jevModel').value.trim(),gcp_project:$('gcpProject').value.trim(),gcp_region:$('gcpRegion').value.trim() || 'global',
    generation_models:editingModels,jev_models:editingJevModels};
  if (!value.generation_model || !value.jev_model) { toast('생성과 진단 모델명을 입력해 주세요.'); return; }
  try {
    if (value.generation_provider === 'openai' && $('openaiKey').value) {
      await api('/api/v2/credentials',{method:'PUT',body:JSON.stringify({provider:'openai',key:$('openaiKey').value})});
      $('openaiKey').value='';
    }
    state.settings=await api('/api/v2/settings',{method:'PUT',body:JSON.stringify(value)}); refreshSettingsLabels(); $('settingsDialog').close(); toast('연결 설정을 저장했어요.');
  }
  catch(error) { toast(error.message); }
});
$('clearOpenaiKey').addEventListener('click',async()=>{
  try { const status=await api('/api/v2/credentials',{method:'DELETE'}); $('openaiKey').value=''; $('openaiKeyStatus').textContent=credentialLabel(status.openai); toast('이번 실행에 입력한 키를 지웠어요.'); }
  catch(error) { toast(error.message); }
});
$('settingsDialog').addEventListener('close',()=>{ $('openaiKey').value=''; });
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',() => $(button.dataset.close).close()));
document.querySelectorAll('dialog').forEach(dialog=>dialog.addEventListener('click',event=>{if(event.target===dialog)dialog.close();}));
$('resetDemo').addEventListener('click',()=>{
  if(state.posting)return; ++state.token; state.selected=null; state.analysis=null; state.busy=false; state.analysisError='';
  $('draftInput').value=''; $('proofInput').value=''; renderVideos(); renderSelection(); setView('studio'); toast('선택을 초기화했어요. 작업 기록은 유지됩니다.');
});
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter'&&!$('postButton').disabled){event.preventDefault();$('postButton').click();}});

(async()=>{
  try { state.settings=await api('/api/v2/settings'); refreshSettingsLabels(); await refreshStatus(); }
  catch(error) { toast(error.message); }
  renderVideos(); renderSelection();
  try {
    const active = await api('/api/v2/rising/active');
    if (active.id) followRising(active.id);
    else { const latest = await api('/api/v2/rising/latest'); if (latest.result) applyRisingResult(latest.result, latest.finished_at); }
  } catch { /* An interrupted search can be started again. */ }
})();
setInterval(() => { if (!state.posting) refreshStatus().catch(() => {}); }, 5000);
