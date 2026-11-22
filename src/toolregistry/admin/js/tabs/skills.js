// ============== Skills ==============
// Agent Skills use progressive disclosure: the catalog carries name and
// description, activation returns instructions, and resources are read on
// demand.  This tab mirrors that flow without editing any skill on disk.
let _skillCatalog = [];
let _activeSkillName = null;
let _activeSkill = null;

async function refreshSkills() {
    const body = document.getElementById('skills-body');
    const summary = document.getElementById('skills-summary');
    try {
        const data = await api.getSkills();
        _skillCatalog = data.skills || [];
        renderSkillCatalog(data);
    } catch (e) {
        summary.textContent = '';
        body.innerHTML = `<div class="empty-state">
            <div class="text-error">${escapeHtml(e.message)}</div>
        </div>`;
    }
}

function renderSkillCatalog(data) {
    const body = document.getElementById('skills-body');
    const summary = document.getElementById('skills-summary');
    const skills = _skillCatalog;
    const tools = data.tools || [];
    const roots = data.roots || [];

    summary.innerHTML = [
        t('skills.count', { count: skills.length }),
        tools.length ? t('skills.tools', { tools: tools.join(', ') }) : '',
    ].filter(Boolean).map(escapeHtml).join(' · ');

    if (skills.length === 0) {
        const rootList = roots.length
            ? `<div style="margin-top:8px;">${roots.map(root =>
                `<code class="ident">${escapeHtml(root.path)}</code>`
            ).join(' ')}</div>`
            : '';
        body.innerHTML = `<div class="empty-state">
            <div class="empty-state-icon">${t('skills.empty')}</div>
            <div class="empty-state-text">${t('skills.emptyHint')}</div>
            ${rootList}
        </div>`;
        return;
    }

    body.innerHTML = skills.map(skill => {
        const name = escapeHtml(skill.name);
        const scope = escapeHtml(skill.scope || 'custom');
        const resourceCount = (skill.resources || []).length;
        return `<div class="source-card">
            <div class="source-card-header">
                <button type="button" class="ns-link" onclick="openSkillDetail('${escapeJsAttr(skill.name)}')">
                    ${name}
                </button>
                <span class="source-tool-chip">${scope}</span>
                <span class="text-muted" style="font-size: var(--text-small); margin-left: auto;">
                    ${t('skills.resourceCount', { count: resourceCount })}
                </span>
            </div>
            <div class="source-detail">${escapeHtml(skill.description || '')}</div>
            <div class="source-card-actions">
                <button class="btn btn-secondary btn-sm"
                        onclick="openSkillDetail('${escapeJsAttr(skill.name)}')">${t('skills.details')}</button>
                <button class="btn btn-primary btn-sm"
                        onclick="activateSkillByName('${escapeJsAttr(skill.name)}')">${t('skills.activate')}</button>
            </div>
        </div>`;
    }).join('');
}

async function openSkillDetail(name) {
    const card = document.getElementById('skill-detail-card');
    const body = document.getElementById('skill-detail-body');
    try {
        const data = await api.getSkill(name);
        _activeSkill = data.skill;
        _activeSkillName = data.skill.name;
        card.style.display = '';
        renderSkillDetail(data.skill);
        card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) {
        showToast(t('skills.detailFailed', { error: e.message }), 'error');
    }
}

function renderSkillDetail(skill) {
    document.getElementById('skill-detail-title').textContent = skill.name;
    const bits = [skill.scope || 'custom'];
    if (skill.path) bits.push(skill.path);
    if (skill.allowed_tools) bits.push(`allowed-tools: ${skill.allowed_tools}`);
    document.getElementById('skill-detail-meta').textContent = bits.join(' · ');

    const resources = skill.resources || [];
    const resourceRows = resources.length
        ? resources.map(resource => `<div style="display:flex;align-items:center;gap:8px;margin:4px 0;">
                <code class="ident">${escapeHtml(resource.path)}</code>
                <span class="text-muted">${escapeHtml(resource.kind)} · ${resource.size} B</span>
                <button class="btn btn-secondary btn-sm"
                        data-path="${escapeHtml(resource.path)}"
                        onclick="readSkillResourceBtn(this)">${t('skills.resourceRead')}</button>
            </div>`).join('')
        : `<div class="text-muted">${t('skills.noResources')}</div>`;

    document.getElementById('skill-detail-body').innerHTML = `
        <div class="detail-section">
            <h4>${t('skills.instructions')}</h4>
            <pre style="background:var(--bg-sunken);color:var(--text-primary);border:1px solid var(--border);padding:12px;border-radius:var(--radius-sm);overflow:auto;white-space:pre-wrap;font-family:var(--font-mono);max-height:40vh;">${escapeHtml(skill.instructions || '')}</pre>
        </div>
        <div class="detail-section" style="margin-top:16px;">
            <h4>${t('skills.resources')}</h4>
            ${resourceRows}
            <pre id="skill-resource-content" style="display:none;background:var(--bg-sunken);color:var(--text-primary);border:1px solid var(--border);padding:12px;border-radius:var(--radius-sm);overflow:auto;white-space:pre-wrap;font-family:var(--font-mono);max-height:40vh;margin-top:8px;"></pre>
        </div>
        <div class="detail-section" style="margin-top:16px;">
            <label style="display:flex;align-items:center;gap:6px;margin-bottom:8px;">
                <input type="checkbox" id="skill-inline-resources">
                <span>${t('skills.inlineResources')}</span>
            </label>
            <pre id="skill-prompt-content" style="display:none;background:var(--bg-sunken);color:var(--text-primary);border:1px solid var(--border);padding:12px;border-radius:var(--radius-sm);overflow:auto;white-space:pre-wrap;font-family:var(--font-mono);max-height:40vh;"></pre>
            <div id="skill-prompt-actions" style="display:none;margin-top:8px;">
                <button class="btn btn-secondary btn-sm" onclick="copySkillPrompt()">${t('btn.copy')}</button>
            </div>
        </div>`;
}

function activateActiveSkill() {
    if (!_activeSkillName) return;
    activateSkillByName(_activeSkillName);
}

async function activateSkillByName(name) {
    const inlineEl = document.getElementById('skill-inline-resources');
    const inline = !!(inlineEl && inlineEl.checked);
    try {
        const data = await api.activateSkill(name, { inline_resources: inline });
        if (_activeSkillName !== name) {
            await openSkillDetail(name);
        }
        const prompt = document.getElementById('skill-prompt-content');
        const actions = document.getElementById('skill-prompt-actions');
        prompt.textContent = data.prompt;
        prompt.style.display = '';
        actions.style.display = '';
        showToast(t('skills.activated', { name }), 'success');
    } catch (e) {
        showToast(t('skills.activateFailed', { error: e.message }), 'error');
    }
}

async function readSkillResourceBtn(button) {
    if (!_activeSkillName) return;
    const path = button.dataset.path;
    try {
        const data = await api.getSkillResource(_activeSkillName, path);
        const pre = document.getElementById('skill-resource-content');
        pre.textContent = data.content;
        pre.style.display = '';
    } catch (e) {
        showToast(t('skills.resourceFailed', { error: e.message }), 'error');
    }
}

async function copySkillPrompt() {
    const prompt = document.getElementById('skill-prompt-content');
    try {
        await navigator.clipboard.writeText(prompt.textContent);
        showToast(t('toast.copied'), 'success');
    } catch (e) {
        showToast(t('toast.copyFailed'), 'error');
    }
}

function closeSkillDetail() {
    document.getElementById('skill-detail-card').style.display = 'none';
    _activeSkillName = null;
    _activeSkill = null;
}
