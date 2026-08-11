// Settings page: the API-keys / providers subset of the index form. Element
// ids are prefixed with s2_ so both pages can never collide; the persisted
// keys match the server's settings_store.ALLOWED_FIELDS names one-to-one.

const SETTINGS_PREFIX = 's2_';

const SETTING_FIELDS = [
    'llm_provider',
    'muapi_key', 'openai_key', 'openai_model', 'gemini_key', 'gemini_model',
    'ollama_url', 'ollama_model', 'nim_key', 'nim_url', 'nim_model',
    'whisper_device', 'whisper_model',
];

const SECRET_MASK = '••••••••';

// Secret fields: a stored key never enters the DOM — the placeholder turns
// into the mask hint instead (same rule as app.js on the index page).
const SECRET_FIELDS = new Set(['muapi_key', 'openai_key', 'gemini_key', 'nim_key']);

function fieldEl(field) {
    return document.getElementById(SETTINGS_PREFIX + field);
}

function applySettings(settings) {
    for (const field of SETTING_FIELDS) {
        const el = fieldEl(field);
        if (!el) continue;
        const value = settings[field];
        if (el.type === 'checkbox') {
            el.checked = value === '1' || value === 1 || value === true || value === 'on' || value === 'true';
            continue;
        }
        if (SECRET_FIELDS.has(field)) {
            // GET returns the mask for a stored key; show it as a hint only.
            // Empty stays empty: leaving the field blank means "don't change".
            const stored = typeof value === 'string' && value !== '';
            el.placeholder = stored ? SECRET_MASK : el.dataset.placeholder || el.placeholder;
            continue;
        }
        if (value !== undefined && value !== null) {
            el.value = String(value);
        }
    }
}

// Mirrors app.js collectSettingsPayload(): checkboxes always travel as '1'/'0'
// so an explicit "off" survives a restart; an empty secret is skipped
// entirely, so it can't clobber the stored key.
function collectSettings() {
    const payload = {};
    for (const field of SETTING_FIELDS) {
        const el = fieldEl(field);
        if (!el) continue;
        if (el.type === 'checkbox') {
            payload[field] = el.checked ? '1' : '0';
            continue;
        }
        const value = String(el.value);
        if (value === '' && SECRET_FIELDS.has(field)) continue;
        payload[field] = value;
    }
    return payload;
}

async function loadSettings() {
    try {
        const resp = await fetch('/api/settings');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        applySettings(await resp.json());
    } catch (e) {
        showToast('Не удалось загрузить настройки', 'error');
    }
}

async function saveSettings(event) {
    const btn = event.target;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Сохранение…';
    try {
        const resp = await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(collectSettings()),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        applySettings(await resp.json()); // re-mask secrets after save
        showToast('Настройки сохранены', 'success');
        btn.textContent = 'Сохранено';
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1500);
    } catch (e) {
        showToast('Не удалось сохранить настройки', 'error');
        btn.textContent = 'Ошибка';
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1500);
    }
}

document.getElementById('s2-save-settings-btn').addEventListener('click', saveSettings);
loadSettings();
