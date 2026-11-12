// ============== API Client ==============
// A 401 opens the sign-in view (js/core/auth.js) once for every request that
// arrives while it is up; a rejected token gets exactly one retry.
class AdminAPI {
    constructor(baseUrl, token) {
        this.baseUrl = baseUrl;
        this.token = token;
        // Whether the server demands credentials.  ``null`` until a response
        // says so: a request that succeeds without a token proves the panel
        // is unauthenticated, a 401 proves it is protected.
        this.authRequired = null;
    }

    getHeaders() {
        const headers = { 'Content-Type': 'application/json' };
        if (this.token) {
            headers['Authorization'] = 'Bearer ' + this.token;
        }
        return headers;
    }

    async request(path, options = {}, authRetried = false) {
        const tokenAtStart = this.token;
        const sentToken = !!this.token;
        // Build headers per attempt and let the current token win:
        // spreading the caller's headers last would re-send the token
        // the server just rejected.  The caller's options are not
        // mutated, so the retry below starts from the same shape.
        const requestOptions = {
            ...options,
            headers: { ...options.headers, ...this.getHeaders() },
        };
        try {
            const response = await fetch(this.baseUrl + path, requestOptions);
            if (response.status === 429) {
                // The server refuses before comparing the credential, so
                // retrying now cannot help.  Show the reason on the sign-in
                // view instead of reporting it inside the panel.
                const message = await this.errorMessage(response);
                showAuthScreen(message);
                throw this.httpError(message, response);
            }
            if (response.status === 401) {
                this.authRequired = true;
                // A stored or freshly entered token that still gets 401
                // is final: never loop the sign-in view forever on a typo.
                if (authRetried) {
                    showAuthScreen(t('auth.invalid'));
                    throw this.httpError(t('error.unauthorized'), response);
                }
                // Another request may have obtained a token while this
                // one was in flight; reuse it instead of asking again.
                let token = this.token;
                if (!token || token === tokenAtStart) {
                    token = await requestToken('');
                    if (!token) throw new Error(t('error.unauthorized'));
                    // Keep the bearer token in memory only.  The server
                    // issues an HttpOnly session cookie on the first
                    // authenticated response, so a reload still
                    // authenticates while page script (and an XSS) cannot
                    // read the long-lived value from storage.
                    this.token = token;
                    AUTH_TOKEN = token;
                }
                return this.request(path, options, true);
            }
            if (response.ok && !sentToken) this.authRequired = false;
            if (!response.ok) {
                throw this.httpError(await this.errorMessage(response), response);
            }
            return await response.json();
        } catch (error) {
            if (error.name === 'TypeError') {
                updateConnectionStatus(false);
                throw new Error(t('error.connectionFailed'));
            }
            throw error;
        }
    }

    // The status travels with the error: an extension has to tell "this
    // feature is not configured" (503) from a failed request, and the message
    // text is not a contract it can branch on.
    httpError(message, response) {
        const error = new Error(message);
        if (response) error.status = response.status;
        return error;
    }

    async errorMessage(response) {
        // The server answers its own errors with JSON (403, 415, 400 ...),
        // but a proxy may return an HTML page.  Show the server's message
        // when there is one and fall back to the status line, rather than
        // surfacing a JSON parse error in place of the reason.
        try {
            const data = await response.json();
            if (data && data.message) return data.message;
        } catch (e) {
            // Not a JSON body; use the status line below.
        }
        return `${response.status} ${response.statusText || ''}`.trim()
            || t('error.requestFailed');
    }

    async getTools() {
        return this.request('/api/tools');
    }

    async getTool(name) {
        return this.request('/api/tools/' + encodeURIComponent(name));
    }

    async enableTool(name) {
        return this.request('/api/tools/' + encodeURIComponent(name) + '/enable', { method: 'POST' });
    }

    async disableTool(name, reason = '') {
        return this.request('/api/tools/' + encodeURIComponent(name) + '/disable', {
            method: 'POST',
            body: JSON.stringify({ reason })
        });
    }

    async getNamespaces() {
        return this.request('/api/namespaces');
    }

    async enableNamespace(ns) {
        return this.request('/api/namespaces/' + encodeURIComponent(ns) + '/enable', { method: 'POST' });
    }

    async disableNamespace(ns, reason = '') {
        return this.request('/api/namespaces/' + encodeURIComponent(ns) + '/disable', {
            method: 'POST',
            body: JSON.stringify({ reason })
        });
    }

    async updateToolMetadata(name, metadata) {
        return this.request('/api/tools/' + encodeURIComponent(name) + '/metadata', {
            method: 'PATCH',
            body: JSON.stringify(metadata)
        });
    }

    async updateNamespaceMetadata(ns, metadata) {
        return this.request('/api/namespaces/' + encodeURIComponent(ns) + '/metadata', {
            method: 'PATCH',
            body: JSON.stringify(metadata)
        });
    }

    async getLogs(params = {}) {
        const query = new URLSearchParams();
        if (params.limit) query.set('limit', params.limit);
        if (params.tool_name) query.set('tool_name', params.tool_name);
        if (params.status) query.set('status', params.status);
        const queryStr = query.toString();
        return this.request('/api/logs' + (queryStr ? '?' + queryStr : ''));
    }

    async getLogStats() {
        return this.request('/api/logs/stats');
    }

    async clearLogs() {
        return this.request('/api/logs', { method: 'DELETE' });
    }

    async exportState() {
        return this.request('/api/state');
    }

    async importState(state) {
        return this.request('/api/state', {
            method: 'POST',
            body: JSON.stringify(state)
        });
    }

    async getPermissions() {
        return this.request('/api/permissions');
    }

    async getSources() {
        return this.request('/api/sources');
    }

    async getSkills() {
        return this.request('/api/skills');
    }

    async getSkill(name) {
        return this.request('/api/skills/' + encodeURIComponent(name));
    }

    async activateSkill(name, options = {}) {
        return this.request(
            '/api/skills/' + encodeURIComponent(name) + '/activate',
            { method: 'POST', body: JSON.stringify(options) }
        );
    }

    async getSkillResource(name, path, maxBytes) {
        const query = maxBytes ? '?max_bytes=' + encodeURIComponent(maxBytes) : '';
        return this.request(
            '/api/skills/' + encodeURIComponent(name)
            + '/resources/' + encodeURIComponent(path) + query
        );
    }

    async previewSource(payload) {
        return this.request('/api/sources/preview', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
    }

    async registerSource(payload) {
        return this.request('/api/sources/register', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
    }

    async getDiscovery() {
        return this.request('/api/discovery');
    }

    async enableDiscovery() {
        return this.request('/api/discovery/enable', { method: 'POST' });
    }

    async disableDiscovery() {
        return this.request('/api/discovery/disable', { method: 'POST' });
    }

    async setDiscoveryMode(mode) {
        return this.request('/api/discovery/mode', {
            method: 'POST',
            body: JSON.stringify({ mode })
        });
    }

    async listOverrides() {
        return this.request('/api/overrides');
    }

    async overrideTool(name, changes) {
        return this.request('/api/tools/' + encodeURIComponent(name) + '/override', {
            method: 'POST',
            body: JSON.stringify(changes),
        });
    }

    async restoreOverride(name) {
        return this.request('/api/tools/' + encodeURIComponent(name) + '/override', {
            method: 'DELETE',
        });
    }

    async getConfig() {
        return this.request('/api/config');
    }

    async updateConfig(data) {
        return this.request('/api/config', {
            method: 'PUT',
            body: JSON.stringify(data)
        });
    }

    async getInfo() {
        return this.request('/api/info');
    }

    async getSchema(format = 'openai-chat') {
        return this.request('/api/schema?format=' + encodeURIComponent(format));
    }
}

const api = new AdminAPI(API_BASE, AUTH_TOKEN);
