// ============== Sign-in, sign-out and the auth gate ==============
// The panel never renders its layout before the server accepts a credential.
// ``<body data-auth>`` drives that: "pending" while the first probe runs,
// "login" while the sign-in view is up, "ok" once authenticated.  CSS hides
// the shell in the first two states, so nothing is visible behind the form.
let _pendingToken = null;

function _authNode(id) {
    return document.getElementById(id);
}

function _setAuthError(message) {
    const node = _authNode('auth-error');
    if (node) node.textContent = message || '';
}

function _setAuthBusy(busy) {
    const button = _authNode('auth-submit');
    const input = _authNode('auth-token');
    if (button) button.disabled = busy;
    if (input) input.disabled = busy;
}

// ---------- Sign-in view preferences ----------
// The panel reads light/dark and the language from localStorage at load, so
// both controls work before a token was accepted.  A sign-in page stuck in
// the wrong language is otherwise a dead end.
const AUTH_EYE_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>';
const AUTH_EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.9 17.9A10.9 10.9 0 0 1 12 19c-7 0-11-7-11-7a20 20 0 0 1 5.1-5.9"/><path d="M9.9 4.2A10.9 10.9 0 0 1 12 4c7 0 11 7 11 7a20 20 0 0 1-2.2 3.2"/><path d="M1 1l22 22"/></svg>';

// Two languages: one button that shows the language in use.  Three or more:
// a menu, because a toggle would cycle through them blindly.  LANGS and
// LANG_LABELS live in i18n.js, so adding a language there is the whole change.
function _renderLangPref() {
    const slot = _authNode('auth-lang-slot');
    if (!slot) return;
    if (LANGS.length > 2) {
        const options = LANGS.map((code) => '<option value="' + code + '"' +
            (code === currentLang ? ' selected' : '') + '>' +
            LANG_LABELS[code] + '</option>').join('');
        slot.innerHTML = '<select class="auth-pref-select" id="auth-lang-select"' +
            ' aria-label="' + t('auth.language') + '"' +
            ' onchange="setLang(this.value); syncAuthPrefs()">' + options + '</select>';
        return;
    }
    slot.innerHTML = '<button class="auth-pref-btn" id="auth-lang" type="button"' +
        ' onclick="toggleLang()" title="' + t('auth.language') + '"' +
        ' aria-label="' + t('auth.language') + '">' + LANG_LABELS[currentLang] +
        '</button>';
}

function toggleLang() {
    const index = LANGS.indexOf(currentLang);
    setLang(LANGS[(index + 1) % LANGS.length]);
}

// Paint the reveal button for the field's current visibility.
function _paintReveal() {
    const input = _authNode('auth-token');
    const button = _authNode('auth-reveal');
    if (!button) return;
    const hidden = !input || input.type !== 'text';
    button.innerHTML = hidden ? AUTH_EYE_OPEN : AUTH_EYE_OFF;
    const label = t(hidden ? 'auth.showToken' : 'auth.hideToken');
    button.title = label;
    button.setAttribute('aria-label', label);
    button.setAttribute('aria-pressed', String(!hidden));
}

function toggleAuthReveal() {
    const input = _authNode('auth-token');
    if (!input) return;
    input.type = input.type === 'text' ? 'password' : 'text';
    _paintReveal();
    input.focus();
}

// A token typed with Caps Lock on is rejected as wrong, which reads as a
// forgotten token rather than a tripped key.
function _watchCapsLock() {
    const input = _authNode('auth-token');
    const warn = _authNode('auth-caps');
    if (!input || !warn || input.dataset.capsWired === '1') return;
    input.dataset.capsWired = '1';
    const sync = (event) => {
        const on = typeof event.getModifierState === 'function' &&
            event.getModifierState('CapsLock');
        warn.hidden = !on;
    };
    input.addEventListener('keydown', sync);
    input.addEventListener('keyup', sync);
    input.addEventListener('blur', () => { warn.hidden = true; });
}

// Repaint everything on the sign-in view that depends on a preference.
function syncAuthPrefs() {
    _renderLangPref();
    syncThemeControls();
    _paintReveal();
}

function showAuthScreen(message) {
    document.body.dataset.auth = 'login';
    const screen = _authNode('auth-screen');
    if (screen) screen.hidden = false;
    _setAuthError(message || '');
    _setAuthBusy(false);
    const input = _authNode('auth-token');
    if (input) {
        input.value = '';
        // A revealed token must not stay readable into the next attempt.
        input.type = 'password';
        // Focus after the state change paints, so a screen reader announces
        // the view rather than an input inside a hidden subtree.
        window.requestAnimationFrame(() => input.focus());
    }
    syncAuthPrefs();
}

function completeAuth() {
    document.body.dataset.auth = 'ok';
    const screen = _authNode('auth-screen');
    if (screen) screen.hidden = true;
    _setAuthError('');
    _setAuthBusy(false);
}

function promptForAdminToken(message) {
    if (_pendingToken) {
        // Several requests hit 401 at once; they all wait on the same view.
        _setAuthError(message || '');
        return _pendingToken.promise;
    }
    showAuthScreen(message);
    let resolve;
    const promise = new Promise((r) => { resolve = r; });
    _pendingToken = { promise, resolve };
    return promise;
}

function submitAuth(event) {
    if (event) event.preventDefault();
    if (!_pendingToken) return;
    const input = _authNode('auth-token');
    const value = ((input && input.value) || '').trim();
    if (!value) {
        _setAuthError(t('auth.empty'));
        if (input) input.focus();
        return;
    }
    const { resolve } = _pendingToken;
    _pendingToken = null;
    _setAuthBusy(true);
    _setAuthError('');
    resolve(value);
}

// Probe the server with whatever credential the browser already has (the
// HttpOnly session cookie) and, while it answers 401 or 429, keep the
// sign-in view up and retry after each submission.  Returns true once
// authenticated.
async function authenticate() {
    let message = '';
    let token; // undefined: the first probe relies on the session cookie only
    for (;;) {
        let response;
        try {
            response = await fetch(API_BASE + '/api/info', {
                headers: authHeaders(token),
                cache: 'no-store',
            });
        } catch (error) {
            token = await promptForAdminToken(t('error.connectionFailed'));
            continue;
        }
        if (response.ok) {
            if (token) {
                api.token = token;
                AUTH_TOKEN = token;
            }
            completeAuth();
            return true;
        }
        if (response.status === 429) {
            // Refused before the credential is compared, so a retry succeeds
            // only after the window passes; say how long that is.
            token = await promptForAdminToken(await lockoutMessage(response));
            continue;
        }
        if (response.status === 401) {
            token = await promptForAdminToken(message);
            message = t('auth.invalid');
            continue;
        }
        // Any other refusal is not something the operator can retry away.
        await promptForAdminToken(await _authFailureMessage(response));
        return false;
    }
}

function authHeaders(token) {
    const headers = {};
    const value = token !== undefined ? token : AUTH_TOKEN;
    if (value) headers['Authorization'] = 'Bearer ' + value;
    return headers;
}

async function lockoutMessage(response) {
    const base = await _authFailureMessage(response);
    const wait = Number(response.headers.get('Retry-After'));
    if (Number.isFinite(wait) && wait > 0) {
        return t('auth.lockedWait').replace('{seconds}', String(Math.ceil(wait)));
    }
    return base || t('auth.locked');
}

async function _authFailureMessage(response) {
    try {
        const data = await response.json();
        if (data && data.message) return data.message;
    } catch (error) {
        // Not JSON; fall through to the generic text.
    }
    return t('auth.locked');
}

async function signOut() {
    // Drop the credential before the request goes out.  Every authenticated
    // response reissues the session cookie, so a request still holding the
    // token after the logout would leave a fresh cookie behind and the panel
    // signed in again on reload.
    const token = AUTH_TOKEN;
    api.token = '';
    AUTH_TOKEN = '';
    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
    }
    try {
        // The server requires a JSON body on unsafe methods (CSRF guard).
        await fetch(API_BASE + '/api/logout', {
            method: 'POST',
            headers: { ...authHeaders(token), 'Content-Type': 'application/json' },
            body: '{}',
        });
    } catch (error) {
        // The cookie is cleared server-side when the request arrives; a
        // network failure still signs the panel out locally.
    }
    // Reload rather than showing the view in place: the panel's data, timers
    // and extension state belong to the ended session, and a reload starts
    // the normal sign-in path from a known-empty document.
    window.location.reload();
}

document.addEventListener('DOMContentLoaded', () => {
    const form = _authNode('auth-form');
    if (form) form.addEventListener('submit', submitAuth);
    const reveal = _authNode('auth-reveal');
    if (reveal) reveal.addEventListener('click', toggleAuthReveal);
    _watchCapsLock();
    syncAuthPrefs();
});
