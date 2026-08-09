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
    'url', 'mode', 'llm_provider', 'num_clips', 'aspect_ratio', 'format', 'language',
    'muapi_key', 'openai_key', 'openai_model', 'gemini_key', 'gemini_model',
    'ollama_url', 'ollama_model', 'nim_key', 'nim_url', 'nim_model',
    'whisper_device', 'whisper_model', 'source_type', 'clip_length',
    'overlay_position', 'overlay_margin', 'overlay_scale', 'use_overlay_opencv',
    'overlay_enabled', 'overlay_x', 'overlay_y',
    'silence_cut', 'blur_bars', 'music_enabled', 'music_file', 'music_volume',
    'captions_enabled', 'caption_style', 'face_track',
];

const SECRET_MASK = '••••••••';

let timerHandle = null;
let activeJobId = null;          // job whose SSE stream is being followed
const polledJobs = {};           // job_id -> latest /api/jobs status snapshot
let firstQueuePollDone = false;

// ---------- Toasts ----------
function showToast(message, type = 'info', ms = 3500) {
    const root = document.getElementById('toast-root');
    if (!root) { console.log(`[${type}] ${message}`); return; }
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = message;
    root.appendChild(el);
    setTimeout(() => el.classList.add('toast-out'), Math.max(0, ms - 300));
    setTimeout(() => el.remove(), ms);
}
window.showToast = showToast;

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

function updateSourceTypeVisibility() {
    const sourceType = document.getElementById('source_type').value;
    const urlGroup = document.getElementById('url-group');
    const fileGroup = document.getElementById('file-group');

    if (sourceType === 'file') {
        urlGroup.classList.add('hidden');
        fileGroup.classList.remove('hidden');
        document.getElementById('url').removeAttribute('required');
        document.getElementById('video_file').setAttribute('required', 'required');
    } else {
        urlGroup.classList.remove('hidden');
        fileGroup.classList.add('hidden');
        document.getElementById('url').setAttribute('required', 'required');
        document.getElementById('video_file').removeAttribute('required');
    }
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
        if (el.type === 'checkbox') {
            el.checked = saved[field] === true || saved[field] === 'true' || saved[field] === '1' || saved[field] === 1;
        } else {
            el.value = saved[field];
        }
    }
    applyOverlaySettings(saved);
    updateMusicVolumeLabel();
    updateMusicFileLabel();
    if (saved.url && !queueUrlInput.value) queueUrlInput.value = saved.url;
    updateSourceTypeVisibility();
    updateVisibleApiGroups();
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
        overlay_margin: document.getElementById('overlay_margin').value,
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
    const volume = parseInt(document.getElementById('music_volume').value, 10);
    return {
        silence_cut: !!document.getElementById('silence_cut').checked,
        blur_bars: !!document.getElementById('blur_bars').checked,
        music_enabled: !!document.getElementById('music_enabled').checked,
        music_file: document.getElementById('music_file').value || '',
        music_volume: isFinite(volume) ? Math.max(0, Math.min(100, volume)) : 40,
        captions_enabled: !!document.getElementById('captions_enabled').checked,
        caption_style: document.getElementById('caption_style').value,
        face_track: !!document.getElementById('face_track').checked,
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
        if (!resp.ok) return;
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
        // Queue polling failing silently is fine -- it is a nice-to-have.
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
    if (!jobs.length) {
        queueList.classList.add('hidden');
        queueList.innerHTML = '';
        return;
    }
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
        num_clips: parseInt(document.getElementById('num_clips').value, 10),
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
        reviewShorts = (data.shorts || []).map(s => ({ ...s, finalized: !!s.finalized })).filter(s => s.url);
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
}

function closeReview() {
    reviewSection.classList.add('hidden');
    reviewDone.classList.add('hidden');
    reviewBody.innerHTML = '';
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
    meta.append(title, details);

    const videoWrap = document.createElement('div');
    const video = document.createElement('video');
    video.className = 'review-video';
    video.controls = true;
    video.playsInline = true;
    video.src = short.url;
    videoWrap.appendChild(video);

    const actions = document.createElement('div');
    actions.className = 'review-actions';

    const saveBtn = document.createElement('button');
    saveBtn.type = 'button';
    saveBtn.className = 'btn-primary';
    saveBtn.textContent = 'Сохранить';
    saveBtn.addEventListener('click', () => {
        showToast('Клип сохранён', 'success');
        advanceReview();
    });

    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    deleteBtn.className = 'btn-secondary btn-danger';
    deleteBtn.textContent = 'Удалить';

    const trimBtn = document.createElement('button');
    trimBtn.type = 'button';
    trimBtn.className = 'btn-secondary';
    trimBtn.textContent = 'Обрезать';

    const finalizeBtn = document.createElement('button');
    finalizeBtn.type = 'button';
    finalizeBtn.className = 'btn-secondary';
    finalizeBtn.textContent = short.finalized ? 'С эффектами' : 'Применить эффекты';
    finalizeBtn.disabled = !!short.finalized;

    actions.append(saveBtn, deleteBtn, trimBtn, finalizeBtn);

    finalizeBtn.addEventListener('click', async () => {
        finalizeBtn.disabled = true;
        const originalText = finalizeBtn.textContent;
        finalizeBtn.innerHTML = '<span class="btn-spinner"></span> Применяю...';
        try {
            const resp = await fetch('/api/shorts/finalize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: short.url }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || !data.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            short.finalized = true;
            const sep = short.url.includes('?') ? '&' : '?';
            video.src = `${short.url}${sep}t=${Date.now()}`;
            video.load();
            finalizeBtn.textContent = 'С эффектами';
            showToast('Эффекты применены', 'success');
        } catch (e) {
            showToast(e.message || 'Не удалось применить эффекты', 'error');
            finalizeBtn.disabled = false;
            finalizeBtn.textContent = originalText;
        }
    });

    deleteBtn.addEventListener('click', async () => {
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

    reviewBody.append(meta, videoWrap, actions, trim.wrap);
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
}

// ---------- Wiring ----------
document.getElementById('mode').addEventListener('change', updateVisibleApiGroups);
document.getElementById('llm_provider').addEventListener('change', updateVisibleApiGroups);
document.getElementById('source_type').addEventListener('change', updateSourceTypeVisibility);

document.getElementById('add-to-queue-btn').addEventListener('click', () => {
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
document.getElementById('review-close-btn').addEventListener('click', closeReview);

// ---------- Processing (silence / blur / music) wiring ----------
document.getElementById('music_volume').addEventListener('input', updateMusicVolumeLabel);
document.getElementById('music_upload_btn').addEventListener('click', async () => {
    const uploadInput = document.getElementById('music_upload');
    const file = uploadInput.files && uploadInput.files[0];
    if (!file) { showToast('Выберите аудиофайл', 'error'); return; }
    const btn = document.getElementById('music_upload_btn');
    btn.disabled = true;
    try {
        const fd = new FormData();
        fd.append('file', file);
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

updateVisibleApiGroups();
updateSourceTypeVisibility();
updateMusicVolumeLabel();
updateMusicFileLabel();
restoreSettings();
pollQueue();
setInterval(pollQueue, 2500);

form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const url = (queueUrlInput.value || '').trim();
    const mode = document.getElementById('mode').value;

    if (!url) {
        showError('Введите YouTube URL.');
        return;
    }
    if (mode !== 'local' && !/^https?:\/\//i.test(url)) {
        // API mode can only reach public URLs; a local path would fail on a path
        // it cannot reach. Same rule the clipboard button enforces.
        showError('Для режима API нужен публичный URL. Переключите режим на «Локальный» для локального файла.');
        return;
    }

    activeJobId = null;
    submitBtn.disabled = true;
    submitBtn.textContent = 'Генерация…';
    resultsSection.classList.add('hidden');
    errorSection.classList.add('hidden');
    progressSection.classList.remove('hidden');
    progressFill.style.width = '0%';
    progressStage.textContent = 'Запуск…';
    pipelineLog.textContent = '';
    startTimer(Date.now());

    const provider = document.getElementById('llm_provider').value || null;

    try {
        const payload = {
            url,
            source_type: 'url',
            mode,
            llm_provider: provider,
            num_clips: parseInt(document.getElementById('num_clips').value, 10),
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
                activeJobId = null;
                finishRun();
                showError(update.error);
                return;
            }

            if (update.line) appendLog(update.line);
            if (typeof update.progress === 'number') {
                progressFill.style.width = `${update.progress}%`;
            }
            if (update.stage) {
                progressStage.textContent = stageLabels[update.stage] || update.stage;
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
        activeJobId = null;
        finishRun();
        showError(error.message);
    }
});

async function pollStatus(jobId) {
    for (let attempt = 0; attempt < 60; attempt++) {
        try {
            const response = await fetch(`/api/status/${jobId}`);
            if (!response.ok) break;
            const job = await response.json();

            if (typeof job.progress === 'number') {
                progressFill.style.width = `${job.progress}%`;
            }
            if (job.status === 'completed' && job.result) {
                finishRun();
                displayResults(job.result, job.elapsed);
                fetchShortsForReview(jobId);
                return;
            }
            if (job.status === 'error') {
                activeJobId = null;
                finishRun();
                showError(job.error || 'Генерация не удалась');
                return;
            }
        } catch (e) {
            console.error('Poll error:', e);
        }
        await new Promise(r => setTimeout(r, 2000));
    }
    activeJobId = null;
    finishRun();
    showError('Потеряно соединение с задачей.');
}

function finishRun() {
    stopTimer();
    progressSection.classList.add('hidden');
    submitBtn.disabled = false;
    submitBtn.textContent = 'Сгенерировать шорты';
}

function displayResults(result, elapsed) {
    resultsGrid.innerHTML = '';
    resultsSection.classList.remove('hidden');

    const summary = document.getElementById('results-summary');
    if (summary) {
        const count = (result.shorts || []).length;
        summary.textContent = elapsed
            ? `${count} клип${count === 1 ? '' : 'ов'} за ${formatElapsed(elapsed)}`
            : `${count} клип${count === 1 ? '' : 'ов'}`;
    }

    (result.shorts || []).forEach((short, index) => {
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
