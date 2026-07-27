const state = {
  projects: [], project: '', videos: [], selected: null, pollTimer: null,
  searchResults: [], resultTab: 'semantic', selectedResult: null, visibleResults: 100,
  projectSequence: 0, videoSequence: 0, searchSequence: 0,
  searchControllers: [], searchLoading: { exact: false, semantic: false },
};

const $ = selector => document.querySelector(selector);
const videoList = $('#videoList');
const player = $('#player');
const playerCard = $('#playerCard');
const playerTitle = $('#playerTitle');
const playerPath = $('#playerPath');
const searchInput = $('#searchInput');
const results = $('#results');

const esc = value => String(value).replace(/[&<>'"]/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
})[character]);

function timecode(seconds) {
  const value = Math.max(0, Math.floor(+seconds || 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor(value % 3600 / 60);
  const rest = value % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
    : `${minutes}:${String(rest).padStart(2, '0')}`;
}

function humanDuration(seconds) {
  const value = Math.max(0, Math.round(+seconds || 0));
  if (value < 60) return `${value} с`;
  const hours = Math.floor(value / 3600);
  const minutes = Math.ceil((value % 3600) / 60);
  return hours ? `${hours} год ${minutes ? `${minutes} хв` : ''}` : `${minutes} хв`;
}

function size(bytes) {
  return bytes < 1024 ** 2 ? `${Math.round(bytes / 1024)} КБ` : `${(bytes / 1024 ** 2).toFixed(1)} МБ`;
}

function label(video) {
  return ({
    ready: 'Готове', queued: 'У черзі', processing: 'Розпізнається…', paused: 'На паузі',
    cancelled: 'Зупинено', done: 'Текст готовий', error: 'Помилка',
  })[video.status] || video.status;
}

function toast(message) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.add('show');
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove('show'), 4200);
}

async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new Error('Сервер не відповідає. Перевір, чи термінал із програмою ще відкритий.');
  }
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`Сервер повернув неочікувану відповідь (${response.status})`);
  }
  if (!response.ok) throw new Error(data.error || `Помилка сервера (${response.status})`);
  return data;
}

async function loadAppInfo() {
  try {
    const info = await api('/api/app');
    $('#appVersion').textContent = `v${info.version}`;
    $('#appVersion').title = info.commit ? `Build ${info.commit.slice(0, 12)}` : '';
  } catch {
    $('#appVersion').textContent = 'версія недоступна';
  }
}

function modelBytes(value) {
  const bytes = Math.max(0, +value || 0);
  if (!bytes) return 'очікую';
  if (bytes < 1024 ** 2) return `${Math.round(bytes / 1024)} КБ`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(0)} МБ`;
  return `${(bytes / 1024 ** 3).toFixed(1)} ГБ`;
}

function renderModelGate(status) {
  const percent = Math.max(0, Math.min(100, Math.round(+status.percent || 0)));
  $('#modelTotalFill').style.width = `${percent}%`;
  $('#modelTotalPercent').textContent = `${percent}%`;
  $('#modelTotalTrack').setAttribute('aria-valuenow', percent);
  $('#modelPhase').textContent = status.phase || 'Перевірка';
  $('#modelRows').innerHTML = (status.models || []).map(model => {
    const modelPercent = Math.max(0, Math.min(100, Math.round(+model.percent || 0)));
    const amount = model.total > 100
      ? `${modelBytes(model.downloaded)} / ${modelBytes(model.total)}`
      : model.detail;
    return `<div class="model-row">
      <div class="model-row-head"><span><strong>${esc(model.label)}</strong><br><small>${esc(amount || '')}</small></span><strong>${modelPercent}%</strong></div>
      <div class="model-track" role="progressbar" aria-label="${esc(model.label)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${modelPercent}"><div style="width:${modelPercent}%"></div></div>
    </div>`;
  }).join('');
  if (status.status === 'error') {
    $('#modelGateTitle').textContent = 'Не вдалося підготувати моделі';
    $('#modelGateCopy').textContent = status.error || 'Перевір з’єднання з інтернетом і спробуй ще раз.';
    $('#retryModels').classList.remove('hidden');
  } else if (status.status === 'downloading') {
    $('#modelGateTitle').textContent = 'Завантажую моделі';
    $('#modelGateCopy').textContent = 'Перший запуск може бути довгим. Не закривай застосунок — прогрес збережеться.';
    $('#retryModels').classList.add('hidden');
  } else {
    $('#modelGateTitle').textContent = 'Перевіряю моделі';
    $('#modelGateCopy').textContent = status.offline
      ? 'Інтернет недоступний, використовую перевірені локальні файли.'
      : 'Звіряю локальні файли з актуальними версіями.';
    $('#retryModels').classList.add('hidden');
  }
}

async function bootstrapModels(retry = false) {
  const gate = $('#modelGate');
  try {
    let status = await api('/api/bootstrap');
    if (status.status === 'idle' || retry) {
      status = await api('/api/bootstrap/start', { method: 'POST' });
    }
    renderModelGate(status);
    if (status.status === 'ready') {
      gate.classList.add('ready');
      await loadProjects();
      return;
    }
    if (status.status === 'error') return;
    setTimeout(() => bootstrapModels(), status.status === 'downloading' ? 500 : 900);
  } catch (error) {
    $('#modelGateTitle').textContent = 'Rothbald не відповідає';
    $('#modelGateCopy').textContent = error.message;
    $('#retryModels').classList.remove('hidden');
  }
}

function recentDate(timestamp) {
  if (!timestamp) return 'Ще не відкривався';
  return new Intl.DateTimeFormat('uk-UA', { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(timestamp * 1000));
}

function currentProject() {
  return state.projects.find(project => project.id === state.project);
}

function renderProjectHome() {
  const box = $('#recentProjects');
  if (!state.projects.length) {
    box.innerHTML = '<div class="home-empty"><h3>Проєктів ще немає</h3><p>Натисни «Новий проєкт» і обери папку з відео.</p></div>';
    return;
  }
  box.innerHTML = state.projects.map(project => {
    const unavailable = !project.folder_available;
    const missing = +project.missing_count || 0;
    const semantic = +project.semantic_busy_count || 0;
    const status = unavailable
      ? 'Папку не знайдено'
      : project.queue_paused
        ? `На паузі: ${project.paused_count}`
        : project.busy_count
          ? `Розпізнається: ${project.busy_count}`
          : semantic
            ? `Індексується зміст: ${semantic}`
            : `Готово: ${project.done_count} із ${project.video_count}`;
    return `<article class="project-card ${unavailable ? 'unavailable' : ''}">
      <button class="project-open" data-open-project="${project.id}">
        <span class="project-icon">${unavailable ? '!' : '▶'}</span>
        <span class="project-copy"><strong>${esc(project.name)}</strong><span class="project-path" title="${esc(project.path)}">${esc(project.path)}</span><span class="project-meta">${status}${missing && !unavailable ? ` · немає файлів: ${missing}` : ''} · ${recentDate(project.last_opened_at)}</span></span>
      </button>
      <button class="locate" data-locate-project="${project.id}">Locate</button>
      <button class="project-delete" data-delete-project="${project.id}" title="Видалити локальний проєкт">×</button>
    </article>`;
  }).join('');
}

async function loadProjects(showErrors = true) {
  try {
    state.projects = await api('/api/projects');
    renderProjectHome();
    if (state.project && state.videos.length) renderQueue();
  } catch (error) {
    if (showErrors) toast(error.message);
    throw error;
  }
}

function showWorkspace() {
  $('#projectHome').classList.add('hidden');
  $('#projectWorkspace').classList.remove('hidden');
  $('#projectActions').classList.remove('hidden');
}

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function cancelSearch() {
  state.searchControllers.forEach(controller => controller.abort());
  state.searchControllers = [];
  state.searchLoading = { exact: false, semantic: false };
}

function showProjectHome() {
  state.projectSequence += 1;
  stopPolling();
  cancelSearch();
  clearVideoSelection(false);
  state.project = '';
  state.videos = [];
  state.searchResults = [];
  state.selectedResult = null;
  $('#projectWorkspace').classList.add('hidden');
  $('#projectHome').classList.remove('hidden');
  results.innerHTML = '<p class="empty">Введи фразу, яку хочеш знайти.</p>';
  $('#resultTabs').classList.add('hidden');
  loadProjects().catch(() => {});
}

async function openProject(id) {
  const sequence = ++state.projectSequence;
  try {
    const data = await api(`/api/projects/${id}/open`, { method: 'POST' });
    if (sequence !== state.projectSequence) return;
    state.project = id;
    state.selected = null;
    state.searchResults = [];
    state.selectedResult = null;
    showWorkspace();
    results.innerHTML = '<p class="empty">Введи фразу, яку хочеш знайти.</p>';
    $('#resultTabs').classList.add('hidden');
    await Promise.all([loadVideos(), loadProjects(false)]);
    if (!data.folder_available) toast('Проєкт відкрито офлайн: текст доступний, а для відео скористайся Locate.');
    else if (data.missing) toast(`Проєкт відкрито. Не знайдено файлів: ${data.missing}`);
  } catch (error) {
    if (sequence === state.projectSequence) toast(error.message);
  }
}

async function locateProject(id) {
  toast('Покажи нове розташування папки у Finder…');
  try {
    const data = await api(`/api/projects/${id}/locate`, { method: 'POST' });
    if (data.cancelled) return;
    await loadProjects(false);
    toast(data.missing ? `Папку прив’язано. Не знайдено файлів: ${data.missing}` : 'Папку знайдено, всі відео на місці.');
    await openProject(id);
  } catch (error) {
    toast(error.message);
  }
}

function processingIsBusy() {
  return state.videos.some(video => video.available && ['queued', 'processing'].includes(video.status))
    || state.videos.some(video => video.segments > 0 && ['pending', 'indexing'].includes(video.semantic_status));
}

function schedulePoll() {
  stopPolling();
  if (!state.project || !processingIsBusy()) return;
  state.pollTimer = setTimeout(() => loadVideos().catch(() => {}), 1200);
}

async function loadVideos() {
  if (!state.project) return;
  const projectAtStart = state.project;
  const sequence = ++state.videoSequence;
  try {
    const videos = await api(`/api/videos?project=${encodeURIComponent(projectAtStart)}`);
    if (projectAtStart !== state.project || sequence !== state.videoSequence) return;
    state.videos = videos;
    renderVideos();
    renderQueue();
    await loadProjects(false);
    schedulePoll();
  } catch (error) {
    if (projectAtStart === state.project) {
      toast(error.message);
      state.pollTimer = setTimeout(() => loadVideos().catch(() => {}), 4000);
    }
  }
}

function videoEta(video) {
  if (video.status !== 'processing' || !video.started_at || video.progress < 0.01) return '';
  const elapsed = Date.now() / 1000 - video.started_at;
  return `≈ ${humanDuration(elapsed * (1 - video.progress) / video.progress)}`;
}

function setProgress(trackSelector, fillSelector, percent) {
  const value = Math.max(0, Math.min(100, Math.round(percent)));
  $(fillSelector).style.width = `${value}%`;
  $(trackSelector).setAttribute('aria-valuenow', value);
  return value;
}

function renderQueue() {
  const box = $('#queueProgress');
  if (box.dataset.projectId !== state.project) {
    box.dataset.projectId = state.project;
    delete box.dataset.autoCollapsed;
    box.open = true;
  }
  const available = state.videos.filter(video => video.available);
  const missing = state.videos.length - available.length;
  if (!state.videos.length) {
    box.classList.add('hidden');
    $('#locateFromQueue').classList.add('hidden');
    $('#searchScope').textContent = 'У цьому проєкті поки немає відео';
    return;
  }
  box.classList.remove('hidden');
  const weights = available.map(video => Math.max(1, +video.duration || 0));
  const total = weights.reduce((sum, value) => sum + value, 0);
  const processed = available.reduce((sum, video, index) => (
    sum + weights[index] * (video.status === 'done' ? 1 : Math.max(0, Math.min(1, +video.progress || 0)))
  ), 0);
  const done = available.filter(video => video.status === 'done').length;
  const queued = available.filter(video => video.status === 'queued').length;
  const paused = available.filter(video => video.status === 'paused').length;
  const errors = available.filter(video => video.status === 'error').length;
  const searchable = state.videos.filter(video => video.segments > 0).length;
  const semanticVideos = state.videos.filter(video => video.segments > 0);
  const semanticReady = semanticVideos.filter(video => video.semantic_status === 'ready').length;
  const active = available.find(video => video.status === 'processing');
  const project = currentProject();
  const queuePaused = Boolean(project && project.queue_paused);
  const transcriptionComplete = Boolean(available.length)
    && done === available.length && !active && !queued && !paused && !errors;
  const semanticComplete = !semanticVideos.length || semanticReady === semanticVideos.length;
  const processingComplete = transcriptionComplete && semanticComplete;
  const allMediaMissing = !available.length && missing > 0;
  const queueLocate = $('#locateFromQueue');
  queueLocate.classList.toggle('hidden', !allMediaMissing);
  if (allMediaMissing) queueLocate.dataset.locateProject = state.project;
  else delete queueLocate.dataset.locateProject;
  box.dataset.state = processingComplete ? 'complete' : errors || allMediaMissing ? 'error' : queuePaused ? 'paused' : 'active';
  $('#queueSummary').textContent = allMediaMissing
    ? 'Файли проєкту зараз недоступні'
    : processingComplete
    ? `${available.length} відео готові до пошуку`
    : queuePaused
      ? 'Обробку призупинено'
      : active || queued
        ? `Розпізнаю матеріали · ${Math.round(total ? processed / total * 100 : 0)}%`
        : 'Готую матеріали до пошуку';
  if (processingComplete && box.dataset.autoCollapsed !== '1') {
    box.open = false;
    box.dataset.autoCollapsed = '1';
  } else if (!processingComplete) {
    box.open = true;
    delete box.dataset.autoCollapsed;
  }
  const retranscribeButton = $('#retranscribe');
  retranscribeButton.disabled = Boolean(active || queued || paused || queuePaused);
  retranscribeButton.title = retranscribeButton.disabled
    ? 'Спочатку заверши, продовж або скинь поточну чергу'
    : 'Заново розпізнати всі доступні відео проєкту';
  const pauseButton = $('#pauseQueue');
  const abortButton = $('#abortQueue');
  pauseButton.textContent = queuePaused ? '▶ Продовжити' : '⏸ Пауза';
  pauseButton.disabled = !queuePaused && !active && !queued;
  abortButton.disabled = !active && !queued && !paused;
  $('#queueControlHint').textContent = queuePaused
    ? 'Продовження піде з останньої повністю готової 30-хвилинної частини.'
    : active || queued ? 'Керує всією чергою цього проєкту.' : 'Черга зараз не запущена.';
  const percent = setProgress('#queueTrack', '#queueFill', total ? processed / total * 100 : 0);
  $('#queueTitle').innerHTML = allMediaMissing
    ? `Текст збережено для <span class="signal-value">${state.videos.length}</span> відео`
    : `Розпізнавання: <span class="signal-value">${done}</span> із <span class="signal-value">${available.length}</span> доступних відео готово`;
  $('#queuePercent').textContent = `${percent}%`;
  $('#queueProcessed').textContent = `Матеріал: ${humanDuration(processed)} із ${humanDuration(total)}${errors ? ` · помилок: ${errors}` : ''}${missing ? ` · файлів немає: ${missing}` : ''}`;

  let eta = '';
  const stalled = active && active.updated_at && Date.now() / 1000 - active.updated_at > 10 * 60;
  if (active && active.started_at && active.progress >= 0.01 && active.duration > 0) {
    const elapsed = Date.now() / 1000 - active.started_at;
    const speed = active.duration * active.progress / Math.max(1, elapsed);
    const remaining = available.reduce((sum, video) => sum + (
      video.status === 'queued' ? video.duration : video.status === 'processing' ? video.duration * (1 - video.progress) : 0
    ), 0);
    if (speed > 0) eta = `Залишилось приблизно ${humanDuration(remaining / speed)}`;
  }
  if (allMediaMissing) eta = 'Підключи диск або скористайся Locate — пошук у тексті вже працює';
  else if (queuePaused) eta = 'Черга на паузі';
  else if (stalled) eta = 'Процес давно не оновлював прогрес — автоматичний контроль зупинить зависання';
  else if (!eta) eta = queued || active ? 'Розраховую час…' : 'Розпізнавання завершене';
  $('#queueEta').textContent = eta;

  const semanticBox = $('#semanticProgress');
  if (semanticVideos.length) {
    semanticBox.classList.remove('hidden');
    const semanticValue = semanticVideos.reduce((sum, video) => {
      if (video.semantic_status === 'ready' && video.semantic_revision === video.transcript_revision) return sum + 1;
      return sum + Math.max(0, Math.min(1, +video.semantic_progress || 0));
    }, 0);
    const semanticPercent = setProgress('#semanticTrack', '#semanticFill', semanticValue / semanticVideos.length * 100);
    semanticBox.dataset.state = semanticComplete ? 'complete' : 'active';
    $('#semanticPercent').textContent = `${semanticPercent}%`;
    $('#semanticTitle').innerHTML = `Індексація змісту: <span class="signal-value">${semanticReady}</span> із <span class="signal-value">${semanticVideos.length}</span> відео готово`;
  } else {
    semanticBox.classList.add('hidden');
    delete semanticBox.dataset.state;
  }
  $('#searchScope').innerHTML = `Пошук по <strong>всіх ${searchable} відео з готовим текстом</strong>${semanticReady < searchable ? `; пошук за змістом готовий для ${semanticReady}` : ''}. Навіть недоступні файли лишаються в пошуку.`;
}

function renderVideos() {
  $('#videoCount').textContent = state.videos.length;
  if (!state.videos.length) {
    videoList.innerHTML = '<p class="empty">У папці поки немає відео.</p>';
    return;
  }
  videoList.innerHTML = state.videos.map(video => {
    const percent = video.status === 'done' ? 100 : Math.round((+video.progress || 0) * 100);
    const eta = videoEta(video);
    const stalled = video.status === 'processing' && video.updated_at && Date.now() / 1000 - video.updated_at > 10 * 60;
    let semantic = '';
    if (video.semantic_status === 'indexing') semantic = `Індексую зміст: ${Math.round((+video.semantic_progress || 0) * 100)}%`;
    else if (video.semantic_status === 'pending' && video.segments > 0) semantic = 'Індексація змісту в черзі';
    else if (video.semantic_status === 'ready') semantic = '';
    else if (video.semantic_status === 'error') semantic = `Помилка індексації: ${video.semantic_error || ''}`;
    const stateLabel = !video.available ? 'Файл не знайдено' : stalled ? 'Можливо зависло' : video.status === 'done' && video.semantic_status === 'ready' ? 'Готово' : label(video);
    return `<div><button class="video-item ${state.selected === video.id ? 'active' : ''} ${!video.available ? 'missing' : ''}" ${video.available ? `data-play="${video.id}"` : ''}>
      <div class="video-name" title="${esc(video.relative_path || video.name)}">${esc(video.name)}</div>
      <div class="video-meta"><span>${humanDuration(video.duration)} · ${size(video.size)}</span><span class="status-${!video.available || stalled ? 'error' : video.status}">${stateLabel}${video.available && video.status === 'processing' ? ` ${percent}%` : ''}</span></div>
      <div class="video-progress ${video.status}" role="progressbar" aria-label="Прогрес ${esc(video.name)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><div style="width:${percent}%"></div></div>
      ${semantic ? `<div class="semantic-state">${esc(semantic)}</div>` : ''}
      ${eta && !stalled && video.available ? `<div class="video-meta"><span>До завершення</span><span>${eta}</span></div>` : ''}
      ${video.error && video.available ? `<div class="video-meta status-${video.status === 'paused' ? 'paused' : 'error'}">${esc(video.error)}</div>` : ''}
    </button>${video.available && ['ready', 'error', 'cancelled'].includes(video.status) ? `<button class="transcribe" data-transcribe="${video.id}">Спробувати ще раз</button>` : ''}</div>`;
  }).join('');
}

function selectVideo(id, at = null) {
  if (at === null) clearResultSelection();
  const video = state.videos.find(item => item.id === id);
  if (video && !video.available) return toast('Текст збережений, але самого відеофайлу немає. Скористайся Locate.');
  if (state.selected === id && at === null) return clearVideoSelection();
  state.selected = id;
  playerTitle.textContent = video?.name || 'Вибране відео';
  playerTitle.title = video?.name || '';
  const relativePath = video?.relative_path || '';
  playerPath.textContent = relativePath && relativePath !== video?.name ? relativePath : '';
  playerPath.title = relativePath;
  playerPath.classList.toggle('hidden', !playerPath.textContent);
  player.src = `/media/${id}`;
  playerCard.classList.remove('empty-player');
  const seek = () => {
    if (at !== null) player.currentTime = Math.max(0, +at - 1.5);
    player.play().catch(() => {});
    player.removeEventListener('loadedmetadata', seek);
  };
  if (at !== null) player.addEventListener('loadedmetadata', seek);
  renderVideos();
}

function clearVideoSelection(render = true) {
  clearResultSelection();
  player.pause();
  player.removeAttribute('src');
  player.load();
  state.selected = null;
  playerTitle.textContent = '';
  playerTitle.removeAttribute('title');
  playerPath.textContent = '';
  playerPath.removeAttribute('title');
  playerCard.classList.add('empty-player');
  if (render) renderVideos();
}

async function chooseFolder() {
  toast('Обери папку нового проєкту у Finder…');
  try {
    const data = await api('/api/projects/choose', { method: 'POST' });
    if (data.cancelled) return;
    toast(data.existing ? 'Цей проєкт уже існує — відкриваю його.' : `Створено проєкт. Знайдено ${data.videos} відео.`);
    await loadProjects(false);
    await openProject(data.id);
  } catch (error) { toast(error.message); }
}

async function rescan() {
  if (!state.project) return;
  try {
    const data = await api(`/api/projects/${state.project}/scan`, { method: 'POST' });
    toast(`Папку оновлено. Знайдено відео: ${data.videos}`);
    await loadVideos();
  } catch (error) { toast(error.message); }
}

async function retranscribeProject() {
  if (!state.project) return;
  const available = state.videos.filter(video => video.available);
  const busy = available.some(video => ['queued', 'processing', 'paused'].includes(video.status)) || Boolean(currentProject()?.queue_paused);
  if (busy) return toast('Спочатку продовж або скинь поточну чергу через Abort.');
  if (!window.confirm(`Заново розпізнати всі ${available.length} доступних відео? Готовий текст заміниться тільки після успішного завершення кожного відео.`)) return;
  try {
    const data = await api(`/api/projects/${state.project}/retranscribe`, { method: 'POST' });
    toast(`У чергу додано відео: ${data.queued}`);
    await loadVideos();
  } catch (error) { toast(error.message); }
}

async function deleteProject(id = state.project) {
  const project = state.projects.find(item => item.id === id);
  if (!project) return;
  if (!window.confirm(`Видалити проєкт «${project.name}» з цієї програми? Локальні транскрипції та індекс буде видалено, але вихідні відео залишаться без змін.`)) return;
  try {
    await api(`/api/projects/${id}`, { method: 'DELETE' });
    toast('Проєкт видалено. Відеофайли не змінювалися.');
    if (state.project === id) showProjectHome();
    else await loadProjects(false);
  } catch (error) { toast(error.message); }
}

async function retry(id) {
  try {
    await api(`/api/videos/${id}/transcribe`, { method: 'POST' });
    toast('Додано в чергу');
    await loadVideos();
  } catch (error) { toast(error.message); }
}

async function togglePause() {
  const project = currentProject();
  const action = project && project.queue_paused ? 'resume' : 'pause';
  try {
    const data = await api(`/api/projects/${state.project}/${action}`, { method: 'POST' });
    toast(action === 'pause'
      ? 'Чергу поставлено на паузу. Готові 30-хвилинні частини збережено.'
      : `Чергу продовжено. Відео в черзі: ${data.queued}`);
    await loadProjects(false);
    await loadVideos();
  } catch (error) { toast(error.message); }
}

async function abortQueue() {
  const unfinished = state.videos.filter(video => ['queued', 'processing', 'paused'].includes(video.status)).length;
  if (!unfinished) return;
  if (!window.confirm(`Зупинити й скинути незавершену чергу (${unfinished} відео)? Контрольні частини буде очищено. Уже готові транскрипції залишаться.`)) return;
  try {
    await api(`/api/projects/${state.project}/abort`, { method: 'POST' });
    toast('Чергу й незавершені контрольні частини скинуто.');
    await loadProjects(false);
    await loadVideos();
  } catch (error) { toast(error.message); }
}

function visibleSearchResults() {
  return state.resultTab === 'all'
    ? state.searchResults
    : state.searchResults.filter(result => result.match_type === state.resultTab);
}

function searchResultKey(result) {
  return [result.match_type, result.video_id, Number(result.start).toFixed(3), Number(result.end).toFixed(3)].join('|');
}

function clearResultSelection() {
  state.selectedResult = null;
  const active = results.querySelector('.result.active');
  if (active) {
    active.classList.remove('active');
    active.setAttribute('aria-pressed', 'false');
  }
}

function renderSearchResults() {
  const semanticCount = state.searchResults.filter(result => result.match_type === 'semantic').length;
  const exactCount = state.searchResults.filter(result => result.match_type === 'exact').length;
  const tabs = $('#resultTabs');
  $('#semanticTabCount').textContent = state.searchLoading.semantic ? '…' : semanticCount;
  $('#exactTabCount').textContent = state.searchLoading.exact ? '…' : exactCount;
  $('#allTabCount').textContent = state.searchLoading.exact || state.searchLoading.semantic ? '…' : state.searchResults.length;
  tabs.classList.toggle('hidden', !state.searchResults.length && !state.searchLoading.exact && !state.searchLoading.semantic);
  tabs.querySelectorAll('[data-result-tab]').forEach(button => button.classList.toggle('active', button.dataset.resultTab === state.resultTab));
  const visible = visibleSearchResults();
  const rendered = visible.slice(0, state.visibleResults);
  $('#resultCount').textContent = visible.length ? `Знайдено ${visible.length}${rendered.length < visible.length ? ` · показано ${rendered.length}` : ''}` : '';
  if (!visible.length) {
    const loading = state.searchLoading[state.resultTab] || (state.resultTab === 'all' && (state.searchLoading.exact || state.searchLoading.semantic));
    results.innerHTML = `<p class="empty">${loading ? 'Шукаю…' : state.resultTab === 'semantic' ? 'Схожих за змістом фрагментів не знайдено.' : 'Точних входжень немає.'}</p>`;
    return;
  }
  results.innerHTML = rendered.map(result => {
    const key = searchResultKey(result);
    const active = state.selectedResult === key;
    return `<button class="result ${result.available ? '' : 'missing'} ${active ? 'active' : ''}" data-video="${result.video_id}" data-time="${result.start}" data-result-key="${esc(key)}" aria-pressed="${active}">
      <span class="time">${timecode(result.start)}</span><span><span class="match-badge ${result.match_type}">${result.match_type === 'exact' ? 'Точний збіг' : 'За змістом'}</span><span class="quote">${esc(result.text)}</span><span class="source">${esc(result.video_name)}${result.available ? '' : ' · файл не знайдено'}</span></span>
    </button>`;
  }).join('');
  if (rendered.length < visible.length) {
    results.insertAdjacentHTML('beforeend', `<div id="resultSentinel" class="result-sentinel">Прокрути нижче — завантажу ще ${Math.min(100, visible.length - rendered.length)} результатів…</div>`);
    const sentinel = $('#resultSentinel');
    const observer = new IntersectionObserver(entries => {
      if (!entries[0].isIntersecting) return;
      observer.disconnect();
      state.visibleResults += 100;
      renderSearchResults();
    }, { rootMargin: '300px' });
    observer.observe(sentinel);
  }
}

async function search() {
  const query = searchInput.value.trim();
  if (!state.project) return toast('Спочатку відкрий проєкт.');
  if (!query) {
    results.innerHTML = '<p class="empty">Введи слова для пошуку.</p>';
    return;
  }
  cancelSearch();
  const sequence = ++state.searchSequence;
  const project = state.project;
  state.searchResults = [];
  state.selectedResult = null;
  state.visibleResults = 100;
  state.resultTab = 'exact';
  state.searchLoading = { exact: true, semantic: true };
  renderSearchResults();
  const params = new URLSearchParams({ q: query, project });

  const run = async type => {
    const controller = new AbortController();
    state.searchControllers.push(controller);
    try {
      const found = await api(`/api/search/${type}?${params}`, { signal: controller.signal });
      if (sequence !== state.searchSequence || project !== state.project) return;
      state.searchResults = state.searchResults.filter(result => result.match_type !== type).concat(found);
      state.searchLoading[type] = false;
      if (type === 'semantic' && found.length && !state.searchResults.some(result => result.match_type === 'exact')) {
        state.resultTab = 'semantic';
      } else if (type === 'exact' && !found.length && !state.searchLoading.semantic) {
        state.resultTab = 'semantic';
      }
      renderSearchResults();
    } catch (error) {
      if (error.name === 'AbortError' || sequence !== state.searchSequence) return;
      state.searchLoading[type] = false;
      toast(error.message);
      renderSearchResults();
    }
  };
  run('exact');
  run('semantic');
}

$('#chooseFolder').addEventListener('click', chooseFolder);
$('#rescan').addEventListener('click', rescan);
$('#retranscribe').addEventListener('click', retranscribeProject);
$('#deleteProject').addEventListener('click', () => deleteProject());
$('#closeProject').addEventListener('click', showProjectHome);
$('#clearSelection').addEventListener('click', () => clearVideoSelection());
$('#pauseQueue').addEventListener('click', togglePause);
$('#abortQueue').addEventListener('click', abortQueue);
$('#recentProjects').addEventListener('click', event => {
  const locate = event.target.closest('[data-locate-project]');
  const remove = event.target.closest('[data-delete-project]');
  const open = event.target.closest('[data-open-project]');
  if (locate) locateProject(locate.dataset.locateProject);
  else if (remove) deleteProject(remove.dataset.deleteProject);
  else if (open) openProject(open.dataset.openProject);
});
videoList.addEventListener('click', event => {
  const retryButton = event.target.closest('[data-transcribe]');
  const play = event.target.closest('[data-play]');
  if (retryButton) retry(retryButton.dataset.transcribe);
  else if (play) selectVideo(play.dataset.play);
});
results.addEventListener('click', event => {
  const result = event.target.closest('[data-video]');
  if (!result) return;
  const key = result.dataset.resultKey;
  if (state.selectedResult === key) {
    clearResultSelection();
    return;
  }
  clearResultSelection();
  state.selectedResult = key;
  result.classList.add('active');
  result.setAttribute('aria-pressed', 'true');
  selectVideo(result.dataset.video, result.dataset.time);
});
$('#resultTabs').addEventListener('click', event => {
  const tab = event.target.closest('[data-result-tab]');
  if (!tab) return;
  state.resultTab = tab.dataset.resultTab;
  state.visibleResults = 100;
  renderSearchResults();
});
$('#searchButton').addEventListener('click', search);
searchInput.addEventListener('keydown', event => { if (event.key === 'Enter') search(); });
$('#retryModels').addEventListener('click', () => bootstrapModels(true));
loadAppInfo();
bootstrapModels();
