// ============== Initialization ==============
// Poll a cheap endpoint so an idle tab still notices a dead server.
// A 401 is left alone: the token prompt owns that path.
async function checkConnection() {
    try {
        const response = await fetch(API_BASE + '/api/info', { headers: api.getHeaders() });
        if (response.ok) updateConnectionStatus(true);
        else if (response.status !== 401) updateConnectionStatus(false);
    } catch (e) {
        updateConnectionStatus(false);
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    initTheme();
    document.getElementById('langSwitcher').value = currentLang;
    applyI18n();
    // Nothing of the panel is rendered until the server accepts a
    // credential: ``authenticate`` either signs in or leaves the sign-in
    // view up (body[data-auth] keeps the shell hidden either way).
    const ready = await authenticate();
    if (!ready) return;
    // Extensions load after the shell is usable: a slow or failing extension
    // must not delay the tools view.
    if (typeof AdminExtensions !== 'undefined') AdminExtensions.load();
    refreshTools();
    refreshStats();
    setInterval(checkConnection, 30000);
});

// Handle keyboard shortcuts
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeModal();
        closeDetailModal();
        toggleConsole(false);
    }
});
