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
globalThis.t = (key) => key;
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
