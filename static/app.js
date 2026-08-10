const form = document.getElementById('generate-form');
const submitBtn = document.getElementById('generate-btn');
const progressSection = document.getElementById('progress-section');
const resultsSection = document.getElementById('results-section');
const errorSection = document.getElementById('error-section');
const progressFill = document.getElementById('progress-fill');
const progressStage = document.getElementById('progress-stage');
const resultsGrid = document.getElementById('results-grid');
const errorMessage = document.getElementById('error-message');
const elapsedTimer = document.getElementById('elapsed-timer');
const pipelineLog = document.getElementById('pipeline-log');

const queueList = document.getElementById('queue-list');
const queueUrlInput = document.getElementById('queue-url');
const reviewSection = document.getElementById('review-section');
const reviewBody = document.getElementById('review-body');
const reviewDone = document.getElementById('review-done');
const reviewCounter = document.getElementById('review-counter');

const stageLabels = {
    starting: 'Запуск…',
    queued: 'В очереди…',
    downloading: 'Скачивание видео…',
    transcribing: 'Транскрибация аудио…',
    analyzing: 'Анализ хайлайтов…',
    rendering: 'Рендеринг клипов…',
    done: 'Готово',
};

const STATUS_LABELS = {
    queued: 'В очереди',
    running: 'Обработка',
    done: 'Готово',
    error: 'Ошибка',
};

// Every field the server persists. Same names on both sides so save/restore
// stays a straight loop rather than a hand-maintained mapping.
const SETTING_FIELDS = [
    'mode', 'llm_provider', 'num_clips', 'aspect_ratio', 'format', 'language',
    'muapi_key', 'openai_key', 'openai_model', 'gemini_key', 'gemini_model',
    'ollama_url', 'ollama_model', 'nim_key', 'nim_url', 'nim_model',
    'whisper_device', 'whisper_model', 'clip_length',
    'overlay_position', 'overlay_margin', 'overlay_scale', 'use_overlay_opencv',
    'overlay_enabled', 'overlay_x', 'overlay_y',
    'silence_cut', 'blur_bars', 'music_enabled', 'music_file', 'music_volume',
    'captions_enabled', 'caption_style', 'face_track',
    'caption_position', 'caption_margin_v',
];

const SECRET_MASK = '••••••••';

// Secret fields whose placeholder turns into a mask hint once a key is stored
// server-side (value stays empty so nothing readable reaches the DOM).
const SECRET_FIELDS = new Set(['muapi_key', 'openai_key', 'gemini_key', 'nim_key']);

let timerHandle = null;
let activeJobId = null;          // job whose SSE stream is being followed
const polledJobs = {};           // job_id -> latest /api/jobs status snapshot
let firstQueuePollDone = false;
let lastKnownQueueSize = 0;      // пустая очередь -> нечего подтверждать
let processingInFlight = false;  // пока true: обе кнопки запуска заблокированы
let serverOffline = false;       // тост о потере связи показываем один раз
let restoredEventSource = null;
let uploadProgress = null;       // span внутри generate-btn, создаётся при первой загрузке

// ---------- Toasts ----------
// У ошибок время жизни длиннее, и у каждого тоста есть кнопка закрытия --
// автоскрытие не должно уносить текст ошибки раньше, чем её прочитали.
function showToast(message, type = 'info', ms = null) {
    const root = document.getElementById('toast-root');
    if (!root) { console.log(`[${type}] ${message}`); return; }
    const timeout = ms || (type === 'error' ? 6000 : 3500);
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message;
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'toast-close';
    closeBtn.setAttribute('aria-label', 'Закрыть');
    closeBtn.textContent = '×';
    const dismiss = () => {
        el.remove();
    };
    closeBtn.addEventListener('click', dismiss);
    el.append(text, closeBtn);
    root.appendChild(el);
    setTimeout(() => el.classList.add('toast-out'), Math.max(0, timeout - 300));
    setTimeout(dismiss, timeout);
}
window.showToast = showToast;

// Тост о потере связи должен появиться на переходе ok -> fail, а не спамить
// при каждой неудачной попытке; на успехе флаг сбрасывается.
function reportServerConnection(ok) {
    if (ok) {
        serverOffline = false;
        return;
    }
    if (!serverOffline) {
        serverOffline = true;
        showToast('Нет связи с сервером — повторная попытка…', 'error', 6000);
    }
}

// Единая точка отрисовки этапа и процента: текст "Этап — 34%", ширина полосы
// и aria-valuenow для скринридеров.
function setProgress(percent, stageText) {
    const stage = stageText || '';
    progressStage.textContent = (typeof percent === 'number')
        ? `${stage.replace(/[…\s]+$/, '')} — ${Math.round(percent)}%`
        : stage;
    if (typeof percent === 'number') {
        progressFill.style.width = `${percent}%`;
        const bar = progressFill.closest('.progress-bar');
        if (bar) bar.setAttribute('aria-valuenow', String(Math.round(percent)));
    }
}

// Пока задача обрабатывается, обе кнопки запуска глушим — иначе легко
// стартовать дубликат той же очереди.
function setProcessingUI(inFlight) {
    processingInFlight = inFlight;
    submitBtn.disabled = inFlight;
    const queueBtn = document.getElementById('add-to-queue-btn');
    if (queueBtn) queueBtn.disabled = inFlight;
}

function formatElapsed(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${String(s).padStart(2, '0')}`;
}

function startTimer(startedAt) {
    stopTimer();
    const tick = () => {
        elapsedTimer.textContent = formatElapsed((Date.now() - startedAt) / 1000);
    };
    tick();
    timerHandle = setInterval(tick, 1000);
}

function stopTimer() {
    if (timerHandle) {
        clearInterval(timerHandle);
        timerHandle = null;
    }
}

function appendLog(line) {
    if (!line) return;
    const atBottom = pipelineLog.scrollTop + pipelineLog.clientHeight >= pipelineLog.scrollHeight - 8;
    pipelineLog.textContent += (pipelineLog.textContent ? '\n' : '') + line;
    if (atBottom) pipelineLog.scrollTop = pipelineLog.scrollHeight;
}

function updateLocalFileVisibility() {
    // Uploading a local file only works in Local mode -- dim (not hide) the
    // section otherwise so the panel doesn't look broken in API mode.
    const active = document.getElementById('mode').value === 'local';
    const sec = document.getElementById('local-file-section');
    if (sec) sec.classList.toggle('inactive', !active);
}

function updateVisibleApiGroups() {
    const mode = document.getElementById('mode').value;
    const llmSelect = document.getElementById('llm_provider');
    const llmHint = document.getElementById('llm-hint');
    const whisperDevice = document.getElementById('whisper_device');
    const whisperModel = document.getElementById('whisper_model');
    const provider = llmSelect.value;

    const apiMode = mode === 'api';
    llmSelect.disabled = apiMode;
    llmHint.style.display = apiMode ? 'inline' : 'none';
    if (apiMode) llmSelect.value = '';

    const localMode = mode === 'local';
    whisperDevice.disabled = !localMode;
    whisperModel.disabled = !localMode;
    const localHints = document.querySelectorAll('#local-options-row .label-hint');
    localHints.forEach(h => h.style.display = localMode ? 'none' : 'inline');

    // Dim the provider blocks that don't apply, rather than hiding them --
    // an empty-looking panel reads as a broken one.
    const activeGroups = apiMode
        ? ['muapi-group']
        : (provider ? [`${provider}-group`]
                    : ['openai-group', 'gemini-group', 'ollama-group', 'nim-group']);

    document.querySelectorAll('.api-group').forEach(group => {
        group.classList.toggle('inactive', !activeGroups.includes(group.id));
    });
    updateLocalFileVisibility();
}

function applyOverlaySettings(saved) {
    const enabled = document.getElementById('overlay_enabled');
    if (saved.overlay_enabled !== undefined) {
        enabled.checked = !(saved.overlay_enabled === 0 || saved.overlay_enabled === '0' ||
                            saved.overlay_enabled === false || saved.overlay_enabled === '' ||
                            saved.overlay_enabled == null);
    }
    const x = document.getElementById('overlay_x');
    const y = document.getElementById('overlay_y');
    const toFrac = (v) => {
        if (v === undefined || v === null || v === '') return '';
        const n = parseFloat(v);
        if (!isFinite(n)) return '';
        return String(Math.max(0, Math.min(1, n)));
    };
    const fx = toFrac(saved.overlay_x);
    const fy = toFrac(saved.overlay_y);
    x.value = (fx !== '' && fy !== '') ? fx : '';
    y.value = (fx !== '' && fy !== '') ? fy : '';
    // Redraw so the preview and the 9-grid selection reflect the loaded state.
    if (typeof drawPreview === 'function') drawPreview();
}

async function restoreSettings() {
    let saved;
    try {
        const response = await fetch('/api/settings');
        if (!response.ok) return;
        saved = await response.json();
    } catch {
        return;  // no saved settings is not an error worth surfacing
    }

    for (const field of SETTING_FIELDS) {
        const el = document.getElementById(field);
        if (!el || saved[field] === undefined || saved[field] === '') continue;
        if (SECRET_FIELDS.has(field) && saved[field] === SECRET_MASK) {
            // Настоящий ключ живёт только на сервере: в DOM кладём пустое
            // значение, а о сохранённости сигнализируем masked-плейсхолдером.
            el.placeholder = `${SECRET_MASK} (сохранено)`;
            continue;
        }
        applyFieldValue(el, saved[field]);
    }
    applyOverlaySettings(saved);
    updateMusicVolumeLabel();
    updateMusicFileLabel();
    if (saved.url && !queueUrlInput.value) queueUrlInput.value = saved.url;
    updateVisibleApiGroups();
    updateLocalFileVisibility();
}

function applyFieldValue(el, value) {
    // Element ids and <script> bodies live in the same template, so a missing
    // element means the HTML drifted -- surface it loudly instead of letting a
    // straggler kill every listener wired after it.
    if (!el) { console.error('restoreSettings: element not found'); return; }
    if (value === undefined || value === '') return;
    if (el.type === 'checkbox') {
        el.checked = value === true || value === 'true' || value === '1' || value === 1;
    } else {
        el.value = value;
    }
}

// Загрузка с прогрессом: fetch не отдаёт upload progress, поэтому XHR.
// Возвращает body ответа как JSON; ошибки бросает наружу.
// Сетевая ошибка помечена `isNetworkError`, чтобы вызывающий мог отличить
// её от серверной HTTP-ошибки и не отправлять файл повторно.
function uploadFileWithProgress(endpoint, formData, onProgress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', endpoint);
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable && onProgress) {
                onProgress(Math.round((e.loaded / e.total) * 100));
            }
        };
        xhr.onload = () => {
            let data = {};
            try { data = JSON.parse(xhr.responseText); } catch {}
            if (xhr.status >= 200 && xhr.status < 300) resolve(data);
            else reject(new Error(data.error || `Upload failed (HTTP ${xhr.status})`));
        };
        xhr.onerror = () => {
            const err = new Error('Upload failed (network error)');
            err.isNetworkError = true;
            reject(err);
        };
        xhr.send(formData);
    });
}

// Resolve which source the run uses: a picked local file wins over the queue
// URL (the file lands under output/uploads/, and its path is sent as `url`
// with source_type='file' -- the same contract /api/generate already speaks).
async function resolveSource() {
    const fileInput = document.getElementById('video_file');
    const file = fileInput && fileInput.files && fileInput.files[0];
    if (file) {
        const fd = new FormData();
        fd.append('video', file);
        let data;
        try {
            if (!uploadProgress) {
                uploadProgress = document.createElement('span');
                uploadProgress.className = 'upload-progress hidden';
                uploadProgress.setAttribute('aria-live', 'polite');
                submitBtn.appendChild(uploadProgress);
            }
            uploadProgress.classList.remove('hidden');
            // Пока файл летит на сервер, на кнопке виден процент — иначе
            // большие файлы выглядят как зависшая страница.
            data = await uploadFileWithProgress('/api/upload', fd, (pct) => {
                uploadProgress.textContent = `Загрузка ${pct}%`;
            });
        } catch (e) {
            // Fallback только при реальной сетевой ошибке XHR: если сервер
            // ответил HTTP-ошибкой, повторный fetch отправил бы файл снова.
            if (!e.isNetworkError && !(e instanceof TypeError)) {
                uploadProgress && uploadProgress.classList.add('hidden');
                throw e;
            }
            uploadProgress && uploadProgress.classList.add('hidden');
            const resp = await fetch('/api/upload', { method: 'POST', body: fd });
            data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `Upload failed (HTTP ${resp.status})`);
        }
        uploadProgress && uploadProgress.classList.add('hidden');
        fileInput.value = '';
        return { url: data.path, source_type: 'file' };
    }
    const url = (queueUrlInput.value || '').trim();
    if (!url) throw new Error('Введите YouTube URL или выберите локальный файл.');
    return { url, source_type: 'url' };
}

// Clamp с пользовательской обратной связью: выход за min/max тихо исправляем,
// но сообщаем — значения вида "-5 клипов" не должны уезжать на сервер.
function clampNumberInput(id, min, max) {
    const el = document.getElementById(id);
    if (!el) return null;
    const n = parseInt(el.value, 10);
    if (!isFinite(n)) return null;
    const clamped = Math.max(min, Math.min(max, n));
    if (clamped !== n) {
        el.value = String(clamped);
        showToast(`Скорректировано: допустимо ${min}–${max}`, 'info');
    }
    return clamped;
}

function collectApiKeys(mode, provider) {
    const read = id => document.getElementById(id).value;
    if (mode === 'api') {
        return read('muapi_key') ? { muapi: read('muapi_key') } : {};
    }

    const byProvider = {
        openai: ['openai_key', 'openai_model'],
        gemini: ['gemini_key', 'gemini_model'],
        ollama: ['ollama_url', 'ollama_model'],
        nim: ['nim_key', 'nim_url', 'nim_model'],
    };
    const keys = {};
    for (const field of byProvider[provider || 'openai'] || []) {
        const value = read(field);
        if (value) keys[field] = value;
    }
    return keys;
}

function collectOverlaySettings() {
    const xRaw = document.getElementById('overlay_x').value;
    const yRaw = document.getElementById('overlay_y').value;
    const x = parseFloat(xRaw);
    const y = parseFloat(yRaw);
    return {
        overlay_position: document.getElementById('overlay_position').value,
        overlay_margin: String(clampNumberInput('overlay_margin', 0, 200) ?? 0),
        overlay_scale: document.getElementById('overlay_scale').value,
        use_overlay_opencv: document.getElementById('use_overlay_opencv').checked ? '1' : '0',
        overlay_enabled: !!document.getElementById('overlay_enabled').checked,
        overlay_x: (xRaw !== '' && isFinite(x)) ? x : null,
        overlay_y: (yRaw !== '' && isFinite(y)) ? y : null,
    };
}

function collectProcessingSettings() {
    // silence_cut / blur_bars are sent for forward compatibility: the generate
    // endpoint ignores them until the backend plumbing lands, but save-settings
    // already persists them.
    const volume = clampNumberInput('music_volume', 0, 100);
    // Empty margin box -> null ("use the backend default") rather than 0.
    const marginRaw = document.getElementById('caption_margin_v').value;
    const margin = marginRaw === '' ? null : clampNumberInput('caption_margin_v', 0, 1200);
    return {
        silence_cut: !!document.getElementById('silence_cut').checked,
        blur_bars: !!document.getElementById('blur_bars').checked,
        music_enabled: !!document.getElementById('music_enabled').checked,
        music_file: document.getElementById('music_file').value || '',
        music_volume: (volume != null) ? volume : 40,
        captions_enabled: !!document.getElementById('captions_enabled').checked,
        caption_style: document.getElementById('caption_style').value,
        face_track: !!document.getElementById('face_track').checked,
        caption_position: document.getElementById('caption_position').value,
        caption_margin_v: margin,
    };
}

function updateMusicVolumeLabel() {
    document.getElementById('music_volume_label').textContent =
        `${document.getElementById('music_volume').value}%`;
}

function updateMusicFileLabel() {
    const path = document.getElementById('music_file').value || '';
    const label = document.getElementById('music_file_label');
    label.textContent = path ? path.split(/[\\/]/).pop() : '';
    label.title = path;
}

// ---------- Queue ----------
async function pollQueue() {
    try {
        const resp = await fetch('/api/jobs');
        if (!resp.ok) { reportServerConnection(false); return; }
        reportServerConnection(true);
        const data = await resp.json();
        const jobs = (data.jobs || []).slice().sort((a, b) => (a.position || 0) - (b.position || 0));
        for (const job of jobs) polledJobs[job.job_id] = job;
        renderQueue(jobs);
        if (firstQueuePollDone && activeJobId) {
            const active = jobs.find(j => j.job_id === activeJobId);
            if (active && (active.status === 'done') && active.has_result &&
                reviewSection.classList.contains('hidden')) {
                fetchShortsForReview(activeJobId);
            }
        }
        firstQueuePollDone = true;
    } catch (e) {
        // Сама очередь — nice-to-have, но потерю связи показать надо.
        reportServerConnection(false);
    }
}

function statusLabel(status) {
    return STATUS_LABELS[status] || status;
}

function jobProgress(job) {
    // Prefer the polled progress when the server provides it, otherwise let the
    // SSE-driven width (stage percent) stand on the running job.
    if (typeof job.progress === 'number') return Math.max(0, Math.min(100, job.progress));
    if (job.status === 'done') return 100;
    if (job.status === 'error') return 100;
    return 0;
}

function renderQueue(jobs) {
    lastKnownQueueSize = jobs.length;
    const emptyState = document.getElementById('queue-empty');
    if (!jobs.length) {
        queueList.classList.add('hidden');
        queueList.innerHTML = '';
        if (emptyState) emptyState.classList.remove('hidden');
        return;
    }
    if (emptyState) emptyState.classList.add('hidden');
    queueList.classList.remove('hidden');
    queueList.innerHTML = '';

    jobs.forEach((job) => {
        const row = document.createElement('div');
        row.className = 'queue-row' + (job.job_id === activeJobId ? ' queue-row-active' : '');

        const pos = document.createElement('span');
        pos.className = 'queue-pos';
        pos.textContent = job.position != null ? `#${job.position}` : '';

        const url = document.createElement('span');
        url.className = 'queue-url';
        url.title = job.url || '';
        url.textContent = job.url || '(без URL)';

        const badge = document.createElement('span');
        badge.className = `queue-badge badge-${job.status}`;
        badge.textContent = statusLabel(job.status);

        const bar = document.createElement('div');
        bar.className = 'queue-progress';
        const fill = document.createElement('div');
        fill.className = 'queue-progress-fill';
        fill.style.width = `${jobProgress(job)}%`;
        bar.appendChild(fill);

        const err = document.createElement('span');
        err.className = 'queue-error';
        if (job.status === 'error' && job.error) err.textContent = job.error;

        row.append(pos, url, badge, bar, err);
        queueList.appendChild(row);
    });
}

async function addToQueue(url) {
    const mode = document.getElementById('mode').value;
    const provider = document.getElementById('llm_provider').value || null;
    const payload = {
        url,
        source_type: 'url',
        mode,
        llm_provider: provider,
        num_clips: clampNumberInput('num_clips', 1, 20) ?? 3,
        clip_length: document.getElementById('clip_length').value,
        aspect_ratio: document.getElementById('aspect_ratio').value,
        format: document.getElementById('format').value,
        language: document.getElementById('language').value || null,
        whisper_device: document.getElementById('whisper_device').value,
        whisper_model: document.getElementById('whisper_model').value,
        api_keys: collectApiKeys(mode, provider),
        ...collectOverlaySettings(),
        ...collectProcessingSettings(),
    };
    try {
        const resp = await fetch('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.error || `HTTP ${resp.status}`);
        }
        const data = await resp.json();
        polledJobs[data.job_id] = { job_id: data.job_id, url, status: 'queued', position: data.position };
        renderQueue(Object.values(polledJobs).sort((a, b) => (a.position || 0) - (b.position || 0)));
        showToast('Задача добавлена в очередь', 'success');
        pollQueue();
    } catch (e) {
        showToast(e.message || 'Не удалось добавить задачу', 'error');
    }
}

// ---------- Review mode ----------
let reviewShorts = [];
let reviewIndex = 0;

function formatBytes(n) {
    if (n == null || isNaN(n)) return '';
    const units = ['Б', 'КБ', 'МБ', 'ГБ'];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

async function fetchShortsForReview(jobId) {
    try {
        const resp = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/shorts`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        reviewShorts = (data.shorts || [])
            .map(s => ({
                ...s,
                finalized: !!s.finalized,
                // Клип уже утверждён, если лежит в output/saved/.
                saved: !!s.saved || String(s.url || '').includes('/output/saved/'),
            }))
            .filter(s => s.url);
        if (!reviewShorts.length) return;
        reviewIndex = 0;
        openReview();
        showToast('Проверка шортов: клипы готовы', 'success');
    } catch (e) {
        showToast('Не удалось загрузить список клипов', 'error');
    }
}

function openReview() {
    reviewSection.classList.remove('hidden');
    reviewDone.classList.add('hidden');
    reviewBody.classList.remove('hidden');
    renderReview();
    // Переносим фокус на карточку: иначе после автоматического открытия
    // ревью клавиатурный пользователь остаётся в другом конце страницы.
    const card = reviewSection.querySelector('.review-card');
    if (card) card.focus();
}

function closeReview() {
    reviewSection.classList.add('hidden');
    reviewDone.classList.add('hidden');
    reviewBody.innerHTML = '';
    // Сбрасываем локальный слепок: при повторном открытии список тянется
    // заново с сервера, чтобы карточки не висели с устаревшим url/saved.
    reviewShorts = [];
    reviewIndex = 0;
}

function formatDuration(sec) {
    if (sec == null || isNaN(sec)) return '';
    return `${Math.round(sec)} с`;
}

function renderReview() {
    const short = reviewShorts[reviewIndex];
    if (!short) { finishReview(); return; }
    reviewCounter.textContent = `Клип ${reviewIndex + 1} из ${reviewShorts.length}`;
    reviewBody.innerHTML = '';
    reviewDone.classList.add('hidden');
    reviewBody.classList.remove('hidden');

    const meta = document.createElement('div');
    meta.className = 'review-meta';
    const title = document.createElement('div');
    title.className = 'review-title';
    title.textContent = short.name || `clip_${reviewIndex + 1}.mp4`;
    const details = document.createElement('div');
    details.className = 'review-details';
    const parts = [];
    if (short.size_bytes != null) parts.push(formatBytes(short.size_bytes));
    if (short.duration_sec != null) parts.push(formatDuration(short.duration_sec));
    details.textContent = parts.join(' · ');
    const badge = document.createElement('span');
    badge.className = 'saved-badge';
    badge.textContent = 'Сохранено';
    if (short.saved) meta.append(title, details, badge); else meta.append(title, details);

    const videoWrap = document.createElement('div');
    videoWrap.className = 'review-videowrap';
    const video = document.createElement('video');
    video.className = 'review-video';
    video.controls = true;
    video.playsInline = true;
    video.src = short.url;
    videoWrap.appendChild(video);

    const actions = document.createElement('div');
    actions.className = 'review-actions';

    // Черновик пока без эффекта и в исходной горизонтальной разметке.
    const hint = document.createElement('p');
    hint.className = 'review-hint';
    hint.textContent = 'Черновик в исходном кадре (16:9), без эффектов — рефрейм под вертикаль и эффекты применятся при сохранении.';

    // Превью: схлопывает controls-конфликт, просто toggle play/pause.
    const previewBtn = document.createElement('button');
    previewBtn.type = 'button';
    previewBtn.className = 'btn-secondary';
    previewBtn.textContent = 'Превью';
    previewBtn.addEventListener('click', () => {
        if (video.paused) { video.play().catch(() => {}); previewBtn.textContent = 'Пауза'; }
        else { video.pause(); previewBtn.textContent = 'Превью'; }
    });
    video.addEventListener('play', () => { previewBtn.textContent = 'Пауза'; });
    video.addEventListener('pause', () => { previewBtn.textContent = 'Превью'; });

    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'btn-primary';
    saveBtn.textContent = '💾 Сохранить';
    saveBtn.disabled = !!short.saved;

    const trimBtn = document.createElement('button');
    trimBtn.type = 'button';
    trimBtn.className = 'btn-secondary';
    trimBtn.textContent = 'Обрезать';

    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    deleteBtn.className = 'btn-secondary btn-danger';
    deleteBtn.textContent = 'Удалить';

    // Cover is generated on demand only — a frame grab per card unconditionally
    // would cost the user ffmpeg runs on clips they might reject anyway.
    const thumbBtn = document.createElement('button');
    thumbBtn.type = 'button';
    thumbBtn.className = 'btn-secondary';
    thumbBtn.textContent = '🖼 Обложка';

    const nextBtn = document.createElement('button');
    nextBtn.type = 'button';
    nextBtn.className = 'btn-secondary';
    nextBtn.textContent = 'Далее';
    nextBtn.addEventListener('click', () => advanceReview());

    actions.append(previewBtn, saveBtn, nextBtn, trimBtn, deleteBtn, thumbBtn);

    saveBtn.addEventListener('click', async () => {
        saveBtn.disabled = true;
        const originalText = saveBtn.textContent;
        saveBtn.innerHTML = '<span class="btn-spinner"></span> Сохраняю...';
        try {
            const resp = await fetch('/api/shorts/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: short.url }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || !data.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            short.saved = true;
            short.finalized = true;
            if (data.url) {
                short.url = data.url;
                const sep = short.url.includes('?') ? '&' : '?';
                video.src = `${short.url}${sep}t=${Date.now()}`;
                video.load();
            }
            if (data.aspect_ratio && /^\s*9\s*:\s*16/.test(data.aspect_ratio)) {
                video.classList.add('review-video-vertical');
            }
            saveBtn.textContent = '💾 Сохранено';
            hint.textContent = 'Сохранено в output/saved/ — рефрейм и эффекты применены.';
            hint.classList.add('review-hint-saved');
            if (!badge.isConnected) meta.append(badge);
            showToast('Клип сохранён в output/saved/', 'success');
            setTimeout(() => advanceReview(), 1100);
        } catch (e) {
            showToast(e.message || 'Не удалось сохранить клип', 'error');
            saveBtn.disabled = false;
            saveBtn.textContent = originalText;
        }
    });

    deleteBtn.addEventListener('click', async () => {
        // Восстановить клип после удаления нельзя — спрашиваем явно.
        if (!window.confirm('Удалить клип без возможности восстановить?')) return;
        deleteBtn.disabled = true;
        try {
            const resp = await fetch('/api/shorts/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: short.url }),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            showToast('Клип удалён', 'success');
            advanceReview();
        } catch (e) {
            showToast('Не удалось удалить клип', 'error');
            deleteBtn.disabled = false;
        }
    });

    const trim = buildTrimForm(short, video);

    trimBtn.addEventListener('click', () => {
        trim.wrap.classList.toggle('hidden');
    });

    reviewBody.append(meta, videoWrap, actions, hint, trim.wrap);

    const thumbWrap = document.createElement('div');
    thumbWrap.className = 'review-thumbnail hidden';

    thumbBtn.addEventListener('click', async () => {
        thumbBtn.disabled = true;
        const originalText = thumbBtn.textContent;
        thumbBtn.innerHTML = '<span class="btn-spinner"></span> Делаю...';
        try {
            const resp = await fetch('/api/shorts/thumbnail', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: short.url, title: short.name }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || !data.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            thumbWrap.innerHTML = '';
            const img = document.createElement('img');
            img.className = 'review-thumbnail-img';
            img.alt = 'Обложка';
            img.src = `${data.url}${data.url.includes('?') ? '&' : '?'}t=${Date.now()}`;
            const link = document.createElement('a');
            link.className = 'review-thumbnail-link';
            link.href = data.url;
            link.download = '';
            link.textContent = '⬇ Скачать обложку';
            thumbWrap.append(img, link);
            thumbWrap.classList.remove('hidden');
            showToast('Обложка готова', 'success');
            thumbBtn.textContent = originalText;
        } catch (e) {
            showToast(e.message || 'Не удалось создать обложку', 'error');
            thumbBtn.textContent = originalText;
        } finally {
            thumbBtn.disabled = false;
        }
    });

    reviewBody.append(thumbWrap);
}

function buildTrimForm(short, video) {
    const wrap = document.createElement('div');
    wrap.className = 'review-trim hidden';

    const grid = document.createElement('div');
    grid.className = 'review-trim-grid';

    const startLbl = document.createElement('label');
    startLbl.textContent = 'Начало (с)';
    const startInput = document.createElement('input');
    startInput.type = 'number';
    startInput.min = '0';
    startInput.step = '0.1';
    startInput.value = '0';

    const endLbl = document.createElement('label');
    endLbl.textContent = 'Конец (с)';
    const endInput = document.createElement('input');
    endInput.type = 'number';
    endInput.min = '0';
    endInput.step = '0.1';
    const dur = (short.duration_sec != null && isFinite(short.duration_sec)) ? short.duration_sec : '';
    endInput.value = dur !== '' ? String(Math.round(dur * 10) / 10) : '';

    grid.append(startLbl, startInput, endLbl, endInput);

    const applyBtn = document.createElement('button');
    applyBtn.type = 'button';
    applyBtn.className = 'btn-primary';
    applyBtn.textContent = 'Применить обрезку';

    applyBtn.addEventListener('click', async () => {
        const start = parseFloat(startInput.value);
        const end = parseFloat(endInput.value);
        if (!isFinite(start) || !isFinite(end) || end <= start) {
            showToast('Укажите корректные начало и конец', 'error');
            return;
        }
        applyBtn.disabled = true;
        try {
            const resp = await fetch('/api/shorts/trim', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: short.url, start_offset: start, end_offset: end }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            if (data.url) {
                short.url = data.url;
                if (data.new_name) short.name = data.new_name;
                video.src = data.url;
                video.load();
            }
            wrap.classList.add('hidden');
            showToast('Обрезка применена', 'success');
        } catch (e) {
            showToast(e.message || 'Не удалось применить обрезку', 'error');
        } finally {
            applyBtn.disabled = false;
        }
    });

    wrap.append(grid, applyBtn);
    return { wrap };
}

function advanceReview() {
    reviewIndex += 1;
    if (reviewIndex >= reviewShorts.length) {
        finishReview();
    } else {
        renderReview();
    }
}

function finishReview() {
    reviewBody.innerHTML = '';
    reviewBody.classList.add('hidden');
    reviewCounter.textContent = '';
    reviewDone.classList.remove('hidden');
    // После ничего не сохранённых клипов «Скачать все» нечего качать.
    const dlAll = document.getElementById('review-download-all-btn');
    if (dlAll) dlAll.disabled = !reviewShorts.some(s => s.saved);
}

// ---------- Wiring ----------
// Every id app.js touches must exist in templates/index.html -- the template
// getElementById/duplicate-id check is enforced by test_gui_features.py.
function wireClick(id, handler) {
    // A missing element here means the id drifted between the template and
    // this script; log it (the template check above fails CI too) and keep
    // wiring the rest so one straggler can't kill the whole page.
    const el = document.getElementById(id);
    if (!el) { console.error(`wireClick: #${id} not found`); return; }
    el.addEventListener('click', handler);
}

function wireChange(id, handler) {
    const el = document.getElementById(id);
    if (!el) { console.error(`wireChange: #${id} not found`); return; }
    el.addEventListener('change', handler);
}

wireChange('mode', updateVisibleApiGroups);
wireChange('llm_provider', updateVisibleApiGroups);

wireClick('add-to-queue-btn', () => {
    if (processingInFlight) { showToast('Дождитесь завершения текущей задачи', 'info'); return; }
    const url = (queueUrlInput.value || '').trim();
    if (!url) { showToast('Введите URL видео', 'error'); return; }
    addToQueue(url);
    queueUrlInput.value = '';
});
queueUrlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        e.preventDefault();
        document.getElementById('add-to-queue-btn').click();
    }
});
wireClick('review-close-btn', closeReview);
wireClick('review-download-all-btn', downloadAllSaved);

// Скачиваем все сохранённые клипы серией скрытых ссылок с download-атрибутом.
// Без Promise.all по fetch: браузер всё равно режет параллельные скачивания,
// а ссылки с паузой достаточно для десятка роликов.
function downloadAllSaved() {
    const saved = reviewShorts.filter(s => s.saved && s.url);
    if (!saved.length) { showToast('Сначала сохраните хотя бы один клип', 'info'); return; }
    saved.forEach((s, i) => {
        setTimeout(() => {
            const a = document.createElement('a');
            a.href = s.url;
            a.download = s.name || `short_${i + 1}.mp4`;
            a.style.display = 'none';
            document.body.appendChild(a);
            a.click();
            a.remove();
        }, i * 400);
    });
    showToast(`Скачиваю ${saved.length} клип(ов)…`, 'success');
}

// Escape закрывает ревью, только когда оно видимо.
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !reviewSection.classList.contains('hidden')) {
        closeReview();
    }
});

// На подтверждение выхода реагируем, только когда есть активная задача --
// иначе предупреждение бесит без пользы.
window.addEventListener('beforeunload', (e) => {
    if (activeJobId || processingInFlight) {
        e.preventDefault();
        e.returnValue = 'Задача ещё обрабатывается — результат не появится, если закрыть вкладку.';
    }
});

// Клэмпы с обратной связью для числовых полей.
const CLAMP_FIELDS = [
    ['num_clips', 1, 20],
    ['caption_margin_v', 0, 1200],
    ['overlay_margin', 0, 200],
    ['music_volume', 0, 100],
];
CLAMP_FIELDS.forEach(([id, min, max]) => wireChange(id, () => clampNumberInput(id, min, max)));

// ---------- Processing (silence / blur / music) wiring ----------
document.getElementById('music_volume').addEventListener('input', updateMusicVolumeLabel);
wireClick('music_upload_btn', async () => {
    const uploadInput = document.getElementById('music_upload');
    const file = uploadInput.files && uploadInput.files[0];
    if (!file) { showToast('Выберите аудиофайл', 'error'); return; }
    const btn = document.getElementById('music_upload_btn');
    btn.disabled = true;
    try {
        const fd = new FormData();
        fd.append('music', file);
        const resp = await fetch('/api/upload/music', { method: 'POST', body: fd });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        document.getElementById('music_file').value = data.path || '';
        document.getElementById('music_file_label').textContent = data.filename || file.name;
        document.getElementById('music_file_label').title = data.path || '';
        showToast('Музыка загружена', 'success');
    } catch (e) {
        showToast(e.message || 'Не удалось загрузить музыку', 'error');
    } finally {
        btn.disabled = false;
    }
});

function ensureResultsEmptyState() {
    if (resultsGrid.children.length) return;
    const empty = document.createElement('div');
    empty.id = 'results-empty';
    empty.className = 'results-empty';
    empty.textContent = 'Нет сохранённых клипов';
    resultsGrid.appendChild(empty);
}

// После перезагрузки страницы активная задача живёт на сервере — находим её
// и снова подключаемся к прогрессу, чтобы экран не выглядел мёртвым.
async function resumeActiveJob() {
    try {
        const resp = await fetch('/api/jobs');
        if (!resp.ok) return null;
        const data = await resp.json();
        const jobs = data.jobs || [];
        // /api/jobs отдаёт running первым, queued за ним — берём первый активный.
        return jobs.find(j => j.status === 'running') ||
               jobs.find(j => j.status === 'queued') || null;
    } catch {
        return null;
    }
}

// Job завершился до перезагрузки — поднимаем готовый результат, чтобы
// страница не выглядела мёртвой.
async function resumeLastCompletedJob() {
    try {
        const resp = await fetch('/api/jobs');
        if (!resp.ok) return null;
        const data = await resp.json();
        const jobs = data.jobs || [];
        const done = jobs.filter(j => j.status === 'done' && j.has_result);
        // sort by finished_at desc if present, else take the last in list
        done.sort((a, b) => (b.finished_at || 0) - (a.finished_at || 0));
        return done[0] || null;
    } catch {
        return null;
    }
}

function followJobProgress(jobId) {
    const es = new EventSource(`/api/progress/${jobId}`);
    restoredEventSource = es;
    es.onmessage = (event) => {
        const update = JSON.parse(event.data);
        if (update.error) {
            es.close();
            restoredEventSource = null;
            finishRun();
            showError(update.error);
            return;
        }
        if (update.line) appendLog(update.line);
        const pct = (typeof update.progress === 'number') ? update.progress : null;
        if (update.stage || pct !== null) {
            setProgress(pct, stageLabels[update.stage] || update.stage || '');
        }
        if (typeof update.elapsed === 'number') {
            elapsedTimer.textContent = formatElapsed(update.elapsed);
        }
        if (update.result) {
            es.close();
            restoredEventSource = null;
            finishRun();
            displayResults(update.result, update.elapsed);
            fetchShortsForReview(jobId);
        }
    };
    es.onerror = () => {
        es.close();
        restoredEventSource = null;
        pollStatus(jobId);
    };
}

updateVisibleApiGroups();
updateLocalFileVisibility();
updateMusicVolumeLabel();
updateMusicFileLabel();
restoreSettings();
ensureResultsEmptyState();
resumeActiveJob().then(async (job) => {
    if (!job) {
        // Нет активной задачи — может, есть готовый результат?
        const doneJob = await resumeLastCompletedJob();
        if (doneJob) {
            displayResults(doneJob.result || {}, doneJob.elapsed);
            fetchShortsForReview(doneJob.job_id);
            pipelineLog.textContent = '';
            setProgress(null, 'Готово.');
        }
        return;
    }
    activeJobId = job.job_id;
    progressSection.classList.remove('hidden');
    resultsSection.classList.add('hidden');
    errorSection.classList.add('hidden');
    submitBtn.textContent = 'Обработка…';
    setProcessingUI(true);
    setProgress(typeof job.progress === 'number' ? job.progress : null,
                stageLabels[job.stage] || 'Обработка…');
    pipelineLog.textContent = '';
    startTimer(job.started_at ? job.started_at * 1000 : Date.now());
    followJobProgress(job.job_id);
    pollQueue();
});
pollQueue();
setInterval(pollQueue, 2500);

form.addEventListener('submit', async (e) => {
    e.preventDefault();

    if (processingInFlight) { showToast('Дождитесь завершения текущей задачи', 'info'); return; }

    // Непустая очередь + отдельная генерация = частый источник дубликатов.
    // Спрашиваем явно, запустить ли задачу вне очереди.
    if (lastKnownQueueSize > 0 &&
        !window.confirm(`В очереди уже ${lastKnownQueueSize} задач. Запустить новую задачу вне очереди?`)) {
        return;
    }

    const mode = document.getElementById('mode').value;

    let source;
    try {
        source = await resolveSource();
    } catch (err) {
        showError(err.message);
        return;
    }
    const { url, source_type } = source;

    if (source_type === 'file' && mode !== 'local') {
        showError('Локальный файл работает только в «Локальном» режиме. Смените режим или уберите файл.');
        return;
    }
    if (source_type === 'url' && mode !== 'local' && !/^https?:\/\//i.test(url)) {
        // API mode can only reach public URLs; a local path would fail on a path
        // it cannot reach. Same rule the clipboard button enforces.
        showError('Для режима API нужен публичный URL. Переключите режим на «Локальный» для локального файла.');
        return;
    }

    setProcessingUI(true);
    submitBtn.textContent = 'Генерация…';
    resultsSection.classList.add('hidden');
    errorSection.classList.add('hidden');
    progressSection.classList.remove('hidden');
    setProgress(0, 'Запуск…');
    pipelineLog.textContent = '';
    startTimer(Date.now());

    const provider = document.getElementById('llm_provider').value || null;

    try {
        const payload = {
            url,
            source_type,
            mode,
            llm_provider: provider,
            num_clips: clampNumberInput('num_clips', 1, 20) ?? 3,
            clip_length: document.getElementById('clip_length').value,
            aspect_ratio: document.getElementById('aspect_ratio').value,
            format: document.getElementById('format').value,
            language: document.getElementById('language').value || null,
            whisper_device: document.getElementById('whisper_device').value,
            whisper_model: document.getElementById('whisper_model').value,
            api_keys: collectApiKeys(mode, provider),
            ...collectOverlaySettings(),
            ...collectProcessingSettings(),
        };

        const response = await fetch('/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.error || 'Не удалось запустить генерацию');
        }

        const { job_id } = await response.json();
        activeJobId = job_id;
        showToast('Задача поставлена в очередь', 'success');
        const eventSource = new EventSource(`/api/progress/${job_id}`);

        eventSource.onmessage = (event) => {
            const update = JSON.parse(event.data);

            if (update.error) {
                eventSource.close();
                finishRun();
                showError(update.error);
                return;
            }

            if (update.line) appendLog(update.line);
            const pct = (typeof update.progress === 'number') ? update.progress : null;
            if (update.stage || pct !== null) {
                setProgress(pct, stageLabels[update.stage] || update.stage || '');
            }
            if (typeof update.elapsed === 'number') {
                elapsedTimer.textContent = formatElapsed(update.elapsed);
            }

            if (update.result) {
                eventSource.close();
                finishRun();
                displayResults(update.result, update.elapsed);
                fetchShortsForReview(job_id);
            }
        };

        eventSource.onerror = () => {
            eventSource.close();
            pollStatus(job_id);
        };
    } catch (error) {
        finishRun();
        showError(error.message);
    }
});

async function pollStatus(jobId) {
    let failedAttempts = 0;
    for (let attempt = 0; attempt < 60; attempt++) {
        try {
            const response = await fetch(`/api/status/${jobId}`);
            if (!response.ok) { failedAttempts += 1; reportServerConnection(false); break; }
            reportServerConnection(true);
            failedAttempts = 0;
            const job = await response.json();

            if (typeof job.progress === 'number') {
                setProgress(job.progress, stageLabels[job.stage] || 'Обработка…');
            }
            if (job.status === 'completed' && job.result) {
                finishRun();
                displayResults(job.result, job.elapsed);
                fetchShortsForReview(jobId);
                return;
            }
            if (job.status === 'error') {
                finishRun();
                showError(job.error || 'Генерация не удалась');
                return;
            }
            // Пока задача в очереди/на выполнении — не считаем это сбоем.
            failedAttempts = 0;
        } catch (e) {
            console.error('Poll error:', e);
            failedAttempts += 1;
            reportServerConnection(false);
        }
        if (failedAttempts > 3) {
            progressStage.textContent = 'Ожидание ответа сервера…';
        }
        await new Promise(r => setTimeout(r, 2000));
    }
    finishRun();
    showError('Потеряно соединение с задачей.');
}

function finishRun() {
    activeJobId = null;
    stopTimer();
    if (restoredEventSource) { restoredEventSource.close(); restoredEventSource = null; }
    progressSection.classList.add('hidden');
    setProcessingUI(false);
    submitBtn.textContent = 'Сгенерировать шорты';
}

function displayResults(result, elapsed) {
    resultsGrid.innerHTML = '';
    resultsSection.classList.remove('hidden');

    const shorts = result.shorts || [];
    if (!shorts.length) {
        const empty = document.createElement('div');
        empty.id = 'results-empty';
        empty.className = 'results-empty';
        empty.textContent = 'Нет сохранённых клипов';
        resultsGrid.appendChild(empty);
    }

    const summary = document.getElementById('results-summary');
    if (summary) {
        const count = shorts.length;
        summary.textContent = elapsed
            ? `${count} клип${count === 1 ? '' : 'ов'} за ${formatElapsed(elapsed)}`
            : `${count} клип${count === 1 ? '' : 'ов'}`;
    }

    shorts.forEach((short, index) => {
        const card = document.createElement('div');
        card.className = 'result-card';

        const header = document.createElement('div');
        header.className = 'result-header';

        const titleWrap = document.createElement('div');
        const title = document.createElement('h3');
        title.className = 'result-title';
        title.textContent = `${index + 1}. ${short.title}`;
        titleWrap.appendChild(title);

        const score = document.createElement('div');
        score.className = 'result-score';
        score.textContent = short.score;

        header.append(titleWrap, score);

        const meta = document.createElement('div');
        meta.className = 'result-meta';
        meta.textContent = `${short.start_time.toFixed(1)}с → ${short.end_time.toFixed(1)}с`;

        const hook = document.createElement('div');
        hook.className = 'result-hook';
        hook.textContent = `"${short.hook_sentence}"`;

        const reason = document.createElement('div');
        reason.className = 'result-reason';
        reason.textContent = short.virality_reason;

        card.append(header, meta, hook, reason);

        if (short.clip_url) {
            const video = document.createElement('video');
            video.className = 'result-video';
            video.controls = true;
            video.src = short.clip_url;

            const download = document.createElement('a');
            download.className = 'btn-download';
            download.textContent = 'Скачать';
            download.href = short.clip_url;
            download.download = `short_${index + 1}.mp4`;
            download.target = '_blank';

            card.append(video, download);
        } else {
            const failed = document.createElement('div');
            failed.className = 'result-failed';
            failed.textContent = `Ошибка: ${short.error || 'неизвестная ошибка'}`;
            card.appendChild(failed);
        }

        resultsGrid.appendChild(card);
    });
}

function showError(message) {
    errorSection.classList.remove('hidden');
    errorMessage.textContent = message;
    showToast(message, 'error');
}
