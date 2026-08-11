// History page: gallery of saved clips from /api/history with search,
// favorites filter, download/delete actions and a video modal.
// Toasts come from common.js (window.showToast); the page URLs it renders are
// already server-relative paths validated by /output/<path>.

const gallery = document.getElementById('history-gallery');
const emptyState = document.getElementById('gallery-empty');
const errorState = document.getElementById('gallery-error');
const retryBtn = document.getElementById('gallery-retry');
const searchInput = document.getElementById('gallery-search');
const filterAllBtn = document.getElementById('filter-all');
const filterFavBtn = document.getElementById('filter-fav');
const videoModal = document.getElementById('video-modal');
const videoPlayer = document.getElementById('video-modal-player');

let allClips = [];
let searchQuery = '';
let showOnlyFavorites = false;

// Same tiers as the review cards on the index page (result-score classes).
function scoreClass(score) {
    if (score >= 75) return 'score-high';
    if (score >= 50) return 'score-mid';
    return 'score-low';
}

function formatDuration(seconds) {
    const total = Math.round(Number(seconds) || 0);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return m > 0 ? `${m}:${String(s).padStart(2, '0')}` : `${total} с`;
}

function formatDate(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const dd = String(d.getDate()).padStart(2, '0');
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    return `${dd}.${mm}.${d.getFullYear()}`;
}

// No thumb and nothing saved on disk: the filename is the only hint left.
function clipTitle(clip) {
    if (clip.title) return clip.title;
    if (clip.saved_url) {
        const name = clip.saved_url.split('/').pop();
        return name.replace(/\.[a-z0-9]+$/i, '');
    }
    return 'Клип';
}

function visibleClips() {
    const q = searchQuery.trim().toLowerCase();
    return allClips.filter((clip) => {
        if (showOnlyFavorites && !clip.favorite) return false;
        if (!q) return true;
        return clipTitle(clip).toLowerCase().includes(q)
            || (clip.source_title || '').toLowerCase().includes(q);
    });
}

function clearCards() {
    gallery.querySelectorAll('.gallery-card, .skeleton').forEach((el) => el.remove());
}

function showSkeletons() {
    clearCards();
    emptyState.hidden = true;
    errorState.hidden = true;
    for (let i = 0; i < 6; i++) {
        const card = document.createElement('div');
        card.className = 'skeleton skeleton-card';
        const thumb = document.createElement('div');
        thumb.className = 'skeleton skeleton-thumb';
        const line = document.createElement('div');
        line.className = 'skeleton skeleton-line';
        const line2 = document.createElement('div');
        line2.className = 'skeleton skeleton-line skeleton-line-short';
        card.append(thumb, line, line2);
        gallery.appendChild(card);
    }
}

function makeCard(clip) {
    const card = document.createElement('div');
    card.className = 'gallery-card';
    card.dataset.id = clip.id;

    if (clip.thumb_url) {
        const img = document.createElement('img');
        img.className = 'gallery-thumb';
        img.src = clip.thumb_url;
        img.alt = clipTitle(clip);
        img.loading = 'lazy';
        if (clip.saved_url) {
            img.addEventListener('click', () => openModal(clip));
        }
        card.appendChild(img);
    } else {
        const ph = document.createElement('button');
        ph.type = 'button';
        ph.className = 'thumb-placeholder';
        ph.textContent = 'Нет превью';
        if (clip.saved_url) ph.addEventListener('click', () => openModal(clip));
        card.appendChild(ph);
    }

    const body = document.createElement('div');
    body.className = 'gallery-body';

    const header = document.createElement('div');
    header.className = 'gallery-header';

    const title = document.createElement('h3');
    title.className = 'gallery-title';
    title.textContent = clipTitle(clip);
    title.title = clipTitle(clip);

    const favBtn = document.createElement('button');
    favBtn.type = 'button';
    favBtn.className = 'fav-btn' + (clip.favorite ? ' is-fav' : '');
    favBtn.setAttribute('aria-pressed', String(!!clip.favorite));
    favBtn.setAttribute('aria-label', 'В избранное');
    favBtn.textContent = clip.favorite ? '♥' : '♡';
    favBtn.addEventListener('click', () => toggleFavorite(clip, favBtn));

    header.append(title, favBtn);
    body.appendChild(header);

    if (clip.score !== null && clip.score !== undefined) {
        const chip = document.createElement('span');
        chip.className = `result-score ${scoreClass(clip.score)}`;
        chip.textContent = clip.score;
        body.appendChild(chip);
    }

    const meta = document.createElement('p');
    meta.className = 'gallery-meta';
    const parts = [];
    if (clip.duration_sec) parts.push(formatDuration(clip.duration_sec));
    const date = formatDate(clip.created_at);
    if (date) parts.push(date);
    meta.textContent = parts.join(' · ');
    body.appendChild(meta);

    card.appendChild(body);

    const actions = document.createElement('div');
    actions.className = 'gallery-actions';

    if (clip.saved_url) {
        const dl = document.createElement('a');
        dl.className = 'btn-secondary gallery-btn';
        dl.href = clip.saved_url;
        dl.setAttribute('download', '');
        dl.textContent = 'Скачать';
        actions.appendChild(dl);
    }

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'btn-secondary gallery-btn gallery-btn-danger';
    del.textContent = 'Удалить';
    del.addEventListener('click', () => deleteClip(clip, card));
    actions.appendChild(del);

    card.appendChild(actions);
    return card;
}

function renderGallery() {
    clearCards();
    const clips = visibleClips();
    emptyState.hidden = clips.length > 0;
    errorState.hidden = true;
    for (const clip of clips) {
        gallery.appendChild(makeCard(clip));
    }
}

async function loadHistory() {
    showSkeletons();
    try {
        const resp = await fetch('/api/history');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        allClips = data.clips || [];
        renderGallery();
    } catch (e) {
        clearCards();
        emptyState.hidden = true;
        errorState.hidden = false;
    }
}

async function toggleFavorite(clip, btn) {
    const prev = !!clip.favorite;
    // Optimistic flip; the filter may drop the card from the grid immediately.
    clip.favorite = !prev;
    btn.textContent = clip.favorite ? '♥' : '♡';
    btn.setAttribute('aria-pressed', String(clip.favorite));
    btn.classList.toggle('is-fav', clip.favorite);
    if (showOnlyFavorites && !clip.favorite) renderGallery();
    try {
        const resp = await fetch('/api/history/favorite', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: clip.id }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const entry = await resp.json();
        clip.favorite = !!entry.favorite;
    } catch (e) {
        clip.favorite = prev; // roll back
        showToast('Не удалось обновить избранное', 'error');
    }
    btn.textContent = clip.favorite ? '♥' : '♡';
    btn.setAttribute('aria-pressed', String(clip.favorite));
    btn.classList.toggle('is-fav', clip.favorite);
}

async function deleteClip(clip, card) {
    if (!confirm('Удалить клип и его файлы? Это действие необратимо.')) return;
    try {
        const resp = await fetch('/api/history/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: clip.id }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        allClips = allClips.filter((c) => c.id !== clip.id);
        card.remove();
        showToast('Клип удалён', 'success');
        renderGallery();
    } catch (e) {
        showToast('Не удалось удалить клип', 'error');
    }
}

function openModal(clip) {
    if (!clip.saved_url) return;
    videoPlayer.src = clip.saved_url;
    videoModal.hidden = false;
    videoPlayer.play().catch(() => { /* autoplay may be blocked */ });
}

function closeModal() {
    videoModal.hidden = true;
    videoPlayer.pause();
    videoPlayer.removeAttribute('src');
    videoPlayer.load();
}

videoModal.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]')) closeModal();
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !videoModal.hidden) closeModal();
});

searchInput.addEventListener('input', () => {
    searchQuery = searchInput.value;
    renderGallery();
});

function setFilter(favoritesOnly) {
    showOnlyFavorites = favoritesOnly;
    filterAllBtn.classList.toggle('chip-active', !favoritesOnly);
    filterFavBtn.classList.toggle('chip-active', favoritesOnly);
    filterFavBtn.setAttribute('aria-pressed', String(favoritesOnly));
    renderGallery();
}

filterAllBtn.addEventListener('click', () => setFilter(false));
filterFavBtn.addEventListener('click', () => setFilter(true));
retryBtn.addEventListener('click', loadHistory);

loadHistory();
