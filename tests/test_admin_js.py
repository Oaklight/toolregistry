"""Behavioural tests for the admin panel's JavaScript auth flow.

The sign-in view lives in ``js/core/auth.js`` and the API client in
``js/core/api.js``.  Both are plain scripts, so these tests load them together
under Node with a small DOM stub and a stubbed ``fetch``.  They skip when Node
is unavailable; GitHub-hosted runners ship it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from importlib.resources import files

ADMIN_API_JS = (
    files("toolregistry.admin").joinpath("js/core/api.js").read_text(encoding="utf-8")
)

ADMIN_AUTH_JS = (
    files("toolregistry.admin").joinpath("js/core/auth.js").read_text(encoding="utf-8")
)

ADMIN_UI_JS = (
    files("toolregistry.admin").joinpath("js/core/ui.js").read_text(encoding="utf-8")
)

ADMIN_EXTENSIONS_JS = (
    files("toolregistry.admin")
    .joinpath("js/core/extensions.js")
    .read_text(encoding="utf-8")
)

ADMIN_I18N_JS = (
    files("toolregistry.admin").joinpath("js/core/i18n.js").read_text(encoding="utf-8")
)

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required")

_HARNESS = """
const vm = require('vm');
const fs = require('fs');

const code = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const goodToken = 'good';

const elements = {};
function node(id) {
  if (!elements[id]) {
    elements[id] = {
      id, hidden: true, value: '', textContent: '', disabled: false,
      innerHTML: '', type: 'password', title: '', placeholder: '',
      dataset: {}, attrs: {},
      focus() {}, addEventListener() {},
      setAttribute(name, value) { this.attrs[name] = value; },
      getAttribute(name) { return this.attrs[name]; },
    };
  }
  return elements[id];
}

globalThis.document = {
  body: { dataset: {} },
  getElementById: node,
  addEventListener() {},
};
globalThis.window = { requestAnimationFrame: (fn) => fn() };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.API_BASE = 'http://example';
globalThis.AUTH_TOKEN = '';
globalThis.autoRefreshInterval = null;
globalThis.updateConnectionStatus = () => {};
// The real page loads theme.js first; the auth tests stub the one function
// auth.js borrows from it.
globalThis.syncThemeControls = () => {};

const calls = [];
globalThis.fetch = async (url, options = {}) => {
  const auth = (options.headers && options.headers.Authorization) || null;
  calls.push(auth);
  const ok = auth === 'Bearer ' + goodToken;
  return {
    status: ok ? 200 : 401,
    ok,
    headers: { get: () => null },
    async json() { return ok ? { ok: true } : { message: 'Unauthorized' }; },
  };
};

vm.runInContext(code, vm.createContext(globalThis));
// i18n.js declares t as a global.  Install the identity stub after the
// script runs so assertions see message keys, not the text of whichever
// language is current.
globalThis.t = (key) => key;

(async () => {
  const api = globalThis.__api;
  const initial = (scenario === 'stale' || scenario === 'wrong') ? 'stale' : '';
  api.token = initial;
  globalThis.AUTH_TOKEN = initial;
  const run = api.request('/probe');
  // Answer the sign-in view once it is up.
  setTimeout(() => {
    node('auth-token').value = scenario === 'wrong' ? 'wrong' : goodToken;
    globalThis.__submitAuth({ preventDefault() {} });
  }, 20);
  let threw = null;
  let threwStatus = null;
  try {
    await run;
  } catch (error) {
    threw = error.message;
    threwStatus = error.status === undefined ? null : error.status;
  }
  console.log(JSON.stringify({
    calls,
    state: globalThis.document.body.dataset.auth,
    error: node('auth-error').textContent,
    threw,
    threwStatus,
  }));
})();
"""


def _combined_source() -> str:
    """Return auth.js and api.js as one script, exporting what the harness needs."""
    return (
        ADMIN_I18N_JS
        + "\n"
        + ADMIN_AUTH_JS
        + "\n"
        + ADMIN_API_JS
        + "\nglobalThis.__api = api;\nglobalThis.__submitAuth = submitAuth;\n"
    )


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (
            "none",
            {
                "calls": [None, "Bearer good"],
                "state": "ok",
                "error": "",
                "threw": None,
                "threwStatus": None,
            },
        ),
        (
            "stale",
            {
                # The rejected token is replaced by the one from the view.
                "calls": ["Bearer stale", "Bearer good"],
                "state": "ok",
                "error": "",
                "threw": None,
                "threwStatus": None,
            },
        ),
        (
            "wrong",
            {
                "calls": ["Bearer stale", "Bearer wrong"],
                # The panel stays on the sign-in view and says why, and the
                # error carries the status an extension branches on.
                "state": "login",
                "error": "auth.invalid",
                "threw": "error.unauthorized",
                "threwStatus": 401,
            },
        ),
    ],
)
def test_the_sign_in_view_drives_the_auth_retry(
    tmp_path: Path, scenario: str, expected: dict
) -> None:
    """A 401 opens the sign-in view; only an accepted token reaches the panel."""
    code_path = tmp_path / "admin_client.js"
    code_path.write_text(_combined_source(), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness), str(code_path), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == expected


def test_the_panel_never_uses_a_native_prompt() -> None:
    """The shell must not block on ``window.prompt`` for credentials."""
    source = ADMIN_AUTH_JS + ADMIN_API_JS
    assert "prompt(" not in source
    assert "auth-screen" in ADMIN_AUTH_JS


_PREFS_HARNESS = """
const vm = require('vm');
const fs = require('fs');

const [, , i18nPath, authPath, extra] = process.argv;

const elements = {};
function node(id) {
  if (!elements[id]) {
    elements[id] = {
      id, hidden: true, value: '', textContent: '', disabled: false,
      innerHTML: '', type: 'password', title: '', placeholder: '',
      dataset: {}, attrs: {},
      focus() {},
      addEventListener() {},
      setAttribute(name, value) { this.attrs[name] = value; },
      getAttribute(name) { return this.attrs[name]; },
    };
  }
  return elements[id];
}

globalThis.document = {
  body: { dataset: {} },
  getElementById: node,
  addEventListener() {},
};
globalThis.window = { requestAnimationFrame: (fn) => fn() };
globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.syncThemeControls = () => {};

vm.createContext(globalThis);
vm.runInContext(fs.readFileSync(i18nPath, 'utf8'), globalThis);
vm.runInContext(fs.readFileSync(authPath, 'utf8'), globalThis);
if (extra) vm.runInContext(extra, globalThis);
vm.runInContext('syncAuthPrefs();', globalThis);

node('auth-token').type = 'password';
vm.runInContext('syncAuthPrefs();', globalThis);
const before = {
  reveal: node('auth-reveal').innerHTML,
  pressed: node('auth-reveal').attrs['aria-pressed'],
};
vm.runInContext('toggleAuthReveal();', globalThis);
const after = {
  type: node('auth-token').type,
  reveal: node('auth-reveal').innerHTML,
  pressed: node('auth-reveal').attrs['aria-pressed'],
};
// The next attempt starts masked, whatever the previous one left behind.
vm.runInContext("showAuthScreen('')", globalThis);
console.log(JSON.stringify({
  lang: node('auth-lang-slot').innerHTML,
  before,
  after,
  reset: {
    type: node('auth-token').type,
    pressed: node('auth-reveal').attrs['aria-pressed'],
  },
}));
"""


def _run_prefs_harness(tmp_path: Path, extra: str | None = None) -> dict:
    i18n_path = tmp_path / "i18n.js"
    i18n_path.write_text(ADMIN_I18N_JS, encoding="utf-8")
    auth_path = tmp_path / "auth.js"
    auth_path.write_text(ADMIN_AUTH_JS, encoding="utf-8")
    harness = tmp_path / "prefs.js"
    harness.write_text(_PREFS_HARNESS, encoding="utf-8")

    command = [NODE, str(harness), str(i18n_path), str(auth_path)]
    command.append(extra or "")
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_sign_in_view_toggles_the_reveal(tmp_path: Path) -> None:
    """The token field can be revealed, and the button reports its state."""
    observed = _run_prefs_harness(tmp_path)
    assert "<svg" in observed["before"]["reveal"]
    assert observed["before"]["pressed"] == "false"
    assert observed["after"]["type"] == "text"
    assert observed["after"]["reveal"] != observed["before"]["reveal"]
    assert observed["after"]["pressed"] == "true"
    # Signing out or a rejected attempt must mask the field again.
    assert observed["reset"]["type"] == "password"
    assert observed["reset"]["pressed"] == "false"


def test_two_languages_render_a_toggle(tmp_path: Path) -> None:
    """With two languages the control is one button showing the current one."""
    observed = _run_prefs_harness(tmp_path)
    assert 'id="auth-lang"' in observed["lang"]
    assert "<select" not in observed["lang"]
    assert ">EN<" in observed["lang"]


def test_more_languages_render_a_menu(tmp_path: Path) -> None:
    """A third language turns the toggle into a menu that lists every choice."""
    observed = _run_prefs_harness(
        tmp_path, extra="LANGS.push('fr'); LANG_LABELS.fr = 'FR';"
    )
    assert "<select" in observed["lang"]
    assert 'value="fr"' in observed["lang"]
    assert 'id="auth-lang"' not in observed["lang"]


def test_register_ui_copy_is_translated_and_wired() -> None:
    """The new Sources hints exist in both dictionaries and are used by the UI."""
    i18n = (
        files("toolregistry.admin")
        .joinpath("js/core/i18n.js")
        .read_text(encoding="utf-8")
    )
    for key in (
        "register.overridesKept",
        "register.stdioNeedsAuth",
        "register.stdioHidden",
        "register.privateHostsRefused",
    ):
        assert i18n.count(f"'{key}':") == 2, key

    register_js = (
        files("toolregistry.admin")
        .joinpath("js/tabs/register.js")
        .read_text(encoding="utf-8")
    )
    assert "preserved_overrides" in register_js
    assert "register.overridesKept" in register_js
    assert "register.stdioNeedsAuth" in register_js
    assert "register.stdioHidden" in register_js
    assert "sourcePrefill" in register_js
    assert "updateStdioHint" in register_js
    assert "register.privateHostsRefused" in register_js
    assert "updatePrivateHostsHint" in register_js
    assert "register-private-hint" in register_js


_REGISTER_PREFILL_HARNESS = """
const vm = require('vm');
const fs = require('fs');

const code = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

const fields = {};
function field(id) {
  if (!fields[id]) {
    const f = { value: '', style: { display: 'none' }, textContent: '' };
    Object.defineProperty(f, 'innerHTML', {
      set() { throw new Error('innerHTML used'); },
    });
    fields[id] = f;
  }
  return fields[id];
}

globalThis.document = { getElementById: (id) => field(id) };
globalThis.t = (key) => (key === 'register.stdioHidden' ? 'HINT {command}' : key);
globalThis.switchTab = () => {};

vm.runInContext(code, vm.createContext(globalThis));
globalThis.openAddSource = () => {};
globalThis.renderRegisterForm = () => {};

const detail =
  scenario === 'xss' ? 'stdio:<img/src=x/onerror=alert(1)>'
  : scenario === 'stdio' ? 'stdio:uvx --api-key SECRET'
  : scenario === 'http' ? 'http://example.com/mcp'
  : '';
globalThis.openRegisterFromSource({
  dataset: { kind: 'mcp', detail, namespace: scenario === 'stdio' ? 'ns' : '' },
});

const hint = field('rf-stdio-prefill-hint');
process.stdout.write(JSON.stringify({
  command: field('rf-command').value,
  url: field('rf-url').value,
  namespace: field('rf-namespace').value,
  hintDisplay: hint.style.display,
  hintText: hint.textContent,
}));
"""


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (
            "stdio",
            {
                "command": "",
                "url": "",
                "namespace": "ns",
                "hintDisplay": "",
                "hintText": "HINT uvx",
            },
        ),
        (
            "xss",
            {
                "command": "",
                "url": "",
                "namespace": "",
                "hintDisplay": "",
                "hintText": "HINT <img/src=x/onerror=alert(1)>",
            },
        ),
        (
            "http",
            {
                "command": "",
                "url": "http://example.com/mcp",
                "namespace": "",
                "hintDisplay": "none",
                "hintText": "",
            },
        ),
        (
            "empty",
            {
                "command": "",
                "url": "",
                "namespace": "",
                "hintDisplay": "none",
                "hintText": "",
            },
        ),
    ],
)
def test_register_stdio_prefill_keeps_the_command_empty(
    tmp_path: Path, scenario: str, expected: dict
) -> None:
    """A masked stdio detail must not become a command value or unsafe HTML."""
    code_path = tmp_path / "register.js"
    code_path.write_text(
        files("toolregistry.admin")
        .joinpath("js/tabs/register.js")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    harness = tmp_path / "harness_prefill.js"
    harness.write_text(_REGISTER_PREFILL_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness), str(code_path), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
    # The stdio detail carries a secret argument on purpose: neither the
    # command field nor the hint may reproduce it.
    assert "SECRET" not in result.stdout


def test_register_prefill_never_copies_source_detail_into_command() -> None:
    """``source_detail`` carries only the program name; it must not fill the field."""
    register_js = (
        files("toolregistry.admin")
        .joinpath("js/tabs/register.js")
        .read_text(encoding="utf-8")
    )
    assert "set('rf-command', detail.slice" not in register_js


_ESCAPE_HARNESS = """
const vm = require('vm');
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const payloads = JSON.parse(process.argv[3]);
vm.runInContext(code, vm.createContext(globalThis));

const entities = { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" };
const results = payloads.map((payload) => {
  const attr = escapeJsAttr(payload);
  // The HTML parser decodes entity references once before the handler is
  // compiled; this is the value the JS parser receives.
  const decoded = attr.replace(/&(amp|lt|gt|quot|#39);/g, (_, key) => entities[key]);
  globalThis.PWNED = 0;
  try {
    new Function(
      'showToolDetailModal',
      "showToolDetailModal('" + decoded + "')"
    )(() => {});
  } catch (error) {
    // A syntax error also proves the payload did not execute.
  }
  return { payload, attr, decoded, pwned: globalThis.PWNED };
});

const html = escapeHtml(
  ['&', '<', '>', String.fromCharCode(34), String.fromCharCode(39)].join('')
);
console.log(JSON.stringify({ results, html }));
"""


def _escape_helpers_source() -> str:
    """Return the escape helpers from ``ui.js`` for the Node harness."""
    start = ADMIN_UI_JS.index("// ============== Utilities ==============")
    end = ADMIN_UI_JS.index("// Prose that names a tool", start)
    return ADMIN_UI_JS[start:end]


@pytest.mark.parametrize(
    "payload",
    [
        "x&#39;);globalThis.PWNED=1;//",
        "x&quot;);globalThis.PWNED=1;//",
        "</script><img src=x onerror=globalThis.PWNED=1>",
        "x');globalThis.PWNED=1;//",
    ],
)
def test_inline_handler_escaping_survives_entity_decoding(
    tmp_path: Path, payload: str
) -> None:
    """A handler argument is parsed as HTML and then as JS; both layers escape.

    ``escapeJs`` alone leaves ``&#39;`` untouched, and the HTML parser turns it
    into a quote before the handler compiles. ``escapeJsAttr`` encodes the
    entity as well.
    """
    code_path = tmp_path / "ui_helpers.js"
    code_path.write_text(_escape_helpers_source(), encoding="utf-8")
    harness = tmp_path / "escape_harness.js"
    harness.write_text(_ESCAPE_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness), str(code_path), json.dumps([payload])],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    entry = data["results"][0]
    assert entry["pwned"] == 0, entry
    assert "'" not in entry["attr"], entry
    assert '"' not in entry["attr"], entry
    assert data["html"] == "&amp;&lt;&gt;&quot;&#39;"


_GROUP_HARNESS = """
const vm = require('vm');
const fs = require('fs');

const i18nPath = process.argv[2];
const extensionsPath = process.argv[3];

const stored = {};
const context = {
  window: {},
  console,
  localStorage: {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(stored, k) ? stored[k] : null),
    setItem: (k, v) => { stored[k] = String(v); },
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(i18nPath, 'utf8'), context, { filename: i18nPath });
vm.runInContext(fs.readFileSync(extensionsPath, 'utf8'), context, { filename: extensionsPath });

function label(key) {
  return vm.runInContext(
    `AdminExtensions.groupLabel(${JSON.stringify(key)})`, context);
}

const en = {
  manage: label('manage'),
  observe: label('observe'),
  unknown: label('unknown-section'),
};

vm.runInContext('currentLang = "zh"', context);
const zh = {
  manage: label('manage'),
  observe: label('observe'),
  unknown: label('unknown-section'),
};

vm.runInContext(
  'currentLang = "en";'
  + 'I18N.en["group.custom"] = "Group custom";'
  + 'I18N.en["nav.custom"] = "Nav custom";',
  context);
const precedence = label('custom');

console.log(JSON.stringify({ en, zh, precedence }));
"""


def test_extension_group_reuses_shell_section_labels(tmp_path: Path) -> None:
    """nav.group = "manage" renders the built-in Manage label in both languages."""
    i18n_path = tmp_path / "i18n.js"
    i18n_path.write_text(ADMIN_I18N_JS, encoding="utf-8")
    extensions_path = tmp_path / "extensions.js"
    extensions_path.write_text(ADMIN_EXTENSIONS_JS, encoding="utf-8")
    harness = tmp_path / "group_harness.js"
    harness.write_text(_GROUP_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness), str(i18n_path), str(extensions_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "en": {
            "manage": "Manage",
            "observe": "Observe",
            "unknown": "unknown-section",
        },
        "zh": {
            "manage": "\u7ba1\u7406",
            "observe": "\u89c2\u5bdf",
            "unknown": "unknown-section",
        },
        "precedence": "Group custom",
    }


_RESTART_HARNESS = """
const vm = require('vm');
const fs = require('fs');

const code = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const goodToken = 'good';

const elements = {};
function node(id) {
  if (!elements[id]) {
    elements[id] = {
      id, hidden: true, value: '', textContent: '', disabled: false,
      innerHTML: '', type: 'password', title: '', placeholder: '',
      dataset: {}, attrs: {},
      focus() {}, addEventListener() {},
      setAttribute(name, value) { this.attrs[name] = value; },
      getAttribute(name) { return this.attrs[name]; },
    };
  }
  return elements[id];
}

let reloaded = false;
const context = vm.createContext(globalThis);
globalThis.document = {
  body: { dataset: {} },
  getElementById: node,
  addEventListener() {},
};
globalThis.window = {
  requestAnimationFrame: (fn) => fn(),
  location: { reload() { reloaded = true; } },
};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.API_BASE = 'http://example';
globalThis.AUTH_TOKEN = '';
globalThis.autoRefreshInterval = null;
globalThis.updateConnectionStatus = () => {};
globalThis.syncThemeControls = () => {};

let locked = scenario === 'lockout';
const calls = [];
globalThis.fetch = async (url, options = {}) => {
  const auth = (options.headers && options.headers.Authorization) || null;
  calls.push(auth);
  if (locked) {
    return {
      status: 429,
      ok: false,
      statusText: 'Too Many Requests',
      headers: { get: (name) => (name === 'Retry-After' ? '1' : null) },
      async json() { return { message: 'Too many failed attempts' }; },
    };
  }
  const ok = auth === 'Bearer ' + goodToken;
  return {
    status: ok ? 200 : 401,
    ok,
    statusText: ok ? 'OK' : 'Unauthorized',
    headers: { get: () => null },
    async json() { return ok ? { ok: true } : { message: 'Unauthorized' }; },
  };
};

vm.runInContext(code, context);
// i18n.js declares t as a global.  Install the identity stub after the
// script runs so assertions see message keys, not translated text.
globalThis.t = (key) => key;

(async () => {
  const api = globalThis.__api;
  if (scenario === 'retried') {
    // The panel holds a stale token; the operator answers the view with a
    // wrong one, and the one permitted retry is rejected too.
    api.token = 'stale';
    globalThis.AUTH_TOKEN = 'stale';
    setTimeout(() => {
      node('auth-token').value = 'wrong';
      globalThis.__submitAuth({ preventDefault() {} });
    }, 10);
  } else {
    // The panel is signed in and a request comes back as a lockout.
    api.token = goodToken;
    globalThis.AUTH_TOKEN = goodToken;
  }
  let firstError = null;
  try {
    await api.request('/probe');
  } catch (error) {
    firstError = {
      message: error.message,
      status: error.status === undefined ? null : error.status,
    };
  }
  const dead = {
    calls: calls.slice(),
    state: document.body.dataset.auth,
    error: node('auth-error').textContent,
    pending: vm.runInContext('_pendingToken === null', context),
    firstError,
  };

  // The terminal failure left the view up but nothing waiting on it.  The
  // next submit must start a fresh probe instead of returning silently.
  locked = false;
  const before = calls.length;
  node('auth-token').value = goodToken;
  globalThis.__submitAuth({ preventDefault() {} });
  await new Promise((resolve) => setTimeout(resolve, 50));
  console.log(JSON.stringify({
    dead,
    extraCalls: calls.slice(before),
    state: document.body.dataset.auth,
    reloaded,
  }));
})();
"""


@pytest.mark.parametrize(
    ("scenario", "status"),
    [
        ("lockout", 429),
        ("retried", 401),
    ],
)
def test_the_sign_in_view_restarts_after_a_terminal_failure(
    tmp_path: Path, scenario: str, status: int
) -> None:
    """A 429 or a rejected retry must leave the form able to start a probe.

    Both terminal paths in ``api.js`` paint the view and throw without opening
    a pending promise, so ``submitAuth`` used to return silently and the page
    was inert until a reload.
    """
    code_path = tmp_path / "admin_client.js"
    code_path.write_text(_combined_source(), encoding="utf-8")
    harness = tmp_path / "restart_harness.js"
    harness.write_text(_RESTART_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness), str(code_path), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed["dead"]["state"] == "login"
    assert observed["dead"]["pending"] is True
    assert observed["dead"]["firstError"]["status"] == status
    # The submit issued a fresh probe with the typed token, and the accepted
    # credential rebuilt the panel from a clean document.
    assert observed["extraCalls"] == ["Bearer good"]
    assert observed["state"] == "ok"
    assert observed["reloaded"] is True


_RESPONSE_HARNESS = """
const vm = require('vm');
const fs = require('fs');

const code = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

function reply(status, statusText, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    statusText,
    headers: { get: () => null },
    async json() {
      if (body instanceof Error) throw body;
      return body;
    },
  };
}

globalThis.API_BASE = 'http://example';
globalThis.AUTH_TOKEN = '';
globalThis.updateConnectionStatus = () => {};
globalThis.completeAuth = () => {};
globalThis.showAuthScreen = () => {};
globalThis.promptForAdminToken = async () => '';
globalThis.t = (key) => key;
globalThis.fetch = async () => {
  if (scenario === 'success') return reply(200, 'OK', { ok: true });
  if (scenario === 'forbidden') {
    return reply(403, 'Forbidden', {
      error: 'Forbidden', message: 'Cross-origin request rejected',
    });
  }
  if (scenario === 'unsupported') {
    return reply(415, 'Unsupported Media Type', {
      error: 'Unsupported Media Type',
      message: 'State-changing requests must use Content-Type: application/json',
    });
  }
  // A proxy may answer with a non-JSON body; only the status line remains.
  return reply(415, 'Unsupported Media Type', new Error('not json'));
};

vm.runInContext(code, vm.createContext(globalThis));

(async () => {
  const api = globalThis.__api;
  let threw = null;
  let threwStatus = null;
  try {
    await api.request('/probe');
  } catch (error) {
    threw = error.message;
    threwStatus = error.status === undefined ? null : error.status;
  }
  console.log(JSON.stringify({
    authRequired: api.authRequired,
    threw,
    threwStatus,
  }));
})();
"""


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("success", {"authRequired": False, "threw": None, "threwStatus": None}),
        (
            "forbidden",
            {
                "authRequired": None,
                "threw": "Cross-origin request rejected",
                "threwStatus": 403,
            },
        ),
        (
            "unsupported",
            {
                "authRequired": None,
                "threw": (
                    "State-changing requests must use Content-Type: application/json"
                ),
                "threwStatus": 415,
            },
        ),
        (
            "html_error",
            {
                "authRequired": None,
                "threw": "415 Unsupported Media Type",
                "threwStatus": 415,
            },
        ),
    ],
)
def test_response_state_and_error_messages(
    tmp_path: Path, scenario: str, expected: dict
) -> None:
    """403/415 messages reach the caller, and auth state is learned from a response."""
    code_path = tmp_path / "admin_api.js"
    code_path.write_text(ADMIN_API_JS + "\nglobalThis.__api = api;\n", encoding="utf-8")
    harness = tmp_path / "response_harness.js"
    harness.write_text(_RESPONSE_HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(harness), str(code_path), scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == expected
