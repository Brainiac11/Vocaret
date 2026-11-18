// Vocaret editor extension.
//
// This side is deliberately thin: it captures where the caret is, tells the
// local vocaret daemon to record / transcribe / ask the model, and applies the
// position-based edits that come back as a single undo step. All of the
// speech and model work lives in the daemon (see ../daemon).

const vscode = require('vscode');
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const VERSION = require('./package.json').version;

/** idle -> starting -> recording -> working -> idle */
let state = 'idle';
let statusItem;
let log;
let flash;
let startSocket = null;   // open connection of the running "start" request
let session = null;       // { editor, document } captured when recording began
let lastVoiceEdit = null; // { uri, version } right after our last edit, for "undo that"
let idleTimer = null;
let extensionContext = null;
let setupRunning = null;  // promise while the speech engine is being installed

// ------------------------------------------------------------------ daemon

function socketPath() {
  if (process.env.VOCARET_SOCKET) return process.env.VOCARET_SOCKET;
  const uid = os.userInfo().uid;
  // Same rule as the daemon: derived from the uid, not $XDG_RUNTIME_DIR,
  // which sandboxed editor builds redirect.
  const runtime = `/run/user/${uid}`;
  if (fs.existsSync(runtime)) return path.join(runtime, 'vocaret.sock');
  return `/tmp/vocaret-${uid}.sock`;
}

function launcherPath() {
  const configured = vscode.workspace.getConfiguration('vocaret').get('command') || 'vocaret';
  if (configured.includes(path.sep)) return configured.replace(/^~(?=\/)/, os.homedir());
  const local = path.join(os.homedir(), '.local', 'bin', configured);
  if (fs.existsSync(local)) return local;
  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    if (dir && fs.existsSync(path.join(dir, configured))) return path.join(dir, configured);
  }
  return null;
}

/** install.sh shipped inside the packaged extension, or the repo's when run from source. */
function installScript() {
  const root = extensionContext.extensionPath;
  for (const candidate of [path.join(root, 'bundled', 'install.sh'), path.join(root, '..', 'install.sh')]) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

function runLogged(command, args) {
  return new Promise((resolve, reject) => {
    log.appendLine(`$ ${command} ${args.join(' ')}`);
    const child = spawn(command, args, { stdio: ['ignore', 'pipe', 'pipe'] });
    const pipe = data => log.append(String(data).replace(/\u001b\[[0-9;]*m/g, ''));
    child.stdout.on('data', pipe);
    child.stderr.on('data', pipe);
    child.on('error', reject);
    child.on('close', code => (code === 0 ? resolve() : reject(new Error(`exit code ${code}`))));
  });
}

/** Install (or update) the Python speech engine + daemon the extension drives. */
function runSetup() {
  if (setupRunning) return setupRunning;
  const script = installScript();
  if (!script) {
    return Promise.reject(new Error('The setup script is missing from this build of the extension.'));
  }
  setupRunning = vscode.window.withProgress({
    location: vscode.ProgressLocation.Notification,
    title: 'Vocaret: setting up the speech engine (one-time download; details in the Vocaret log)',
  }, async () => {
    try { await request({ op: 'shutdown' }, () => {}); } catch (err) { /* not running */ }
    await runLogged('bash', [script, '--no-editors', '--no-check']);
    const launcher = launcherPath();
    if (!launcher) throw new Error('Setup finished but the vocaret launcher was not found.');
    // Fetch the speech model now rather than during the first instruction.
    await runLogged(launcher, ['prepare']);
  }).then(() => { setupRunning = null; }, err => {
    setupRunning = null;
    log.show(true);
    throw new Error(`Setup failed (${err.message}). See the Vocaret log for details.`);
  });
  return setupRunning;
}

async function offerSetup(reason, action) {
  const choice = await vscode.window.showInformationMessage(
    `Vocaret: ${reason} This installs a private Python environment in ~/.local/share/vocaret `
    + '(about 0.4 GB, or about 2.5 GB with NVIDIA GPU support) and downloads a Whisper speech model.',
    { modal: true }, action);
  if (choice !== action) throw new Error('Setup was skipped. Run "Vocaret: Install or Update Speech Engine" when ready.');
  await runSetup();
}

/** Send one request; call onEvent per event; resolve when the daemon closes. */
function request(req, onEvent, onSocket) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath());
    let buffer = '';
    let opened = false;
    if (onSocket) onSocket(socket);
    socket.setEncoding('utf8');
    socket.on('connect', () => {
      opened = true;
      socket.write(JSON.stringify(req) + '\n');
    });
    socket.on('data', chunk => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        try { onEvent(JSON.parse(line)); } catch (err) { log.appendLine(`bad event: ${err}`); }
      }
    });
    socket.on('error', err => { err.notRunning = !opened; reject(err); });
    socket.on('close', () => resolve());
  });
}

async function daemonStatus() {
  let status = null;
  try {
    await request({ op: 'status' }, ev => { if (ev.event === 'status') status = ev; });
  } catch (err) {
    return null;
  }
  return status;
}

async function startDaemon() {
  const launcher = launcherPath();
  log.appendLine(`starting daemon: ${launcher} daemon`);
  let failure = null;
  try {
    const child = spawn(launcher, ['daemon'], { detached: true, stdio: 'ignore' });
    child.on('error', err => { failure = err; });
    child.unref();
  } catch (err) {
    failure = err;
  }
  for (let i = 0; i < 100 && !failure; i++) {
    await sleep(150);
    const status = await daemonStatus();
    if (status) return status;
  }
  throw new Error(`Vocaret's background service didn't start${failure ? `: ${failure.message}` : ''}. `
    + 'Try "Vocaret: Check Setup".');
}

async function stopDaemon() {
  try { await request({ op: 'shutdown' }, () => {}); } catch (err) { /* already gone */ }
  await sleep(500);
}

/** Make sure a daemon of this extension's version is running, installing it if needed. */
async function ensureDaemon() {
  if (process.platform === 'win32') {
    throw new Error('Vocaret supports Linux (and macOS experimentally) for now, not Windows.');
  }
  let status = await daemonStatus();
  if (status && status.version === VERSION) return;
  if (status) await stopDaemon(); // left over from another version
  if (!launcherPath()) {
    await offerSetup('the speech engine is not installed yet.', 'Set Up');
  }
  status = await startDaemon();
  if (status.version !== VERSION) {
    await stopDaemon();
    await offerSetup(`the installed speech engine is version ${status.version}, this extension is ${VERSION}.`, 'Update');
    status = await startDaemon();
    if (status.version !== VERSION) {
      throw new Error(`The speech engine is still version ${status.version} after updating; expected ${VERSION}.`);
    }
  }
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

const secretName = provider => `vocaret.apiKey.${provider}`;

async function options() {
  const cfg = vscode.workspace.getConfiguration('vocaret');
  const provider = cfg.get('provider') || 'claude-code';
  const opts = {
    provider,
    model: cfg.get('model'),
    effort: cfg.get('effort'),
    base_url: cfg.get('baseUrl'),
    api_key_env: cfg.get('apiKeyEnv'),
    extra_body: cfg.get('extraBody'),
    timeout: cfg.get('timeoutSeconds'),
    silence_seconds: cfg.get('autoStopSilenceSeconds'),
    whisper_model: cfg.get('speechModel'),
    device: cfg.get('device'),
    language: cfg.get('language'),
    recorder_command: cfg.get('recorderCommand'),
  };
  // Keys live in the editor's secret storage and travel only over the
  // user-private local socket to the daemon.
  const key = await extensionContext.secrets.get(secretName(provider));
  if (key) opts.api_key = key;
  return opts;
}

// ------------------------------------------------------- providers and keys

async function listProviders() {
  await ensureDaemon();
  let providers = [];
  await request({ op: 'providers' }, ev => { if (ev.event === 'providers') providers = ev.providers; });
  return providers;
}

async function setApiKey(providerArg, keyArg) {
  const cfg = vscode.workspace.getConfiguration('vocaret');
  const provider = typeof providerArg === 'string' ? providerArg : cfg.get('provider');
  const info = (await listProviders()).find(p => p.id === provider);
  if (info && !info.needs_key && provider !== 'custom') {
    vscode.window.showInformationMessage(`Vocaret: ${info.label} does not use an API key.`);
    return false;
  }
  const key = typeof keyArg === 'string' ? keyArg : await vscode.window.showInputBox({
    title: `API key for ${info ? info.label : provider}`,
    prompt: "Stored in your editor's secret storage, not in settings.",
    password: true,
    ignoreFocusOut: true,
  });
  if (!key || !key.trim()) return false;
  await extensionContext.secrets.store(secretName(provider), key.trim());
  if (typeof keyArg !== 'string') {
    vscode.window.showInformationMessage(`Vocaret: API key saved for ${info ? info.label : provider}.`);
  }
  return true;
}

async function clearApiKey() {
  const provider = vscode.workspace.getConfiguration('vocaret').get('provider');
  await extensionContext.secrets.delete(secretName(provider));
  vscode.window.showInformationMessage(`Vocaret: stored API key for "${provider}" removed.`);
}

async function selectProvider() {
  let providers;
  try {
    providers = await listProviders();
  } catch (err) {
    return fail(err);
  }
  const cfg = vscode.workspace.getConfiguration('vocaret');
  const current = cfg.get('provider');
  const picked = await vscode.window.showQuickPick(providers.map(p => ({
    label: (p.id === current ? '$(check) ' : '') + p.label,
    description: p.needs_key ? 'API key' : (p.kind === 'openai' ? 'local / no key' : 'subscription login'),
    detail: p.detail,
    provider: p,
  })), { title: 'Vocaret: which model service should write the code?', matchOnDetail: true });
  if (!picked) return;
  const p = picked.provider;
  const target = vscode.ConfigurationTarget.Global;

  if (p.needs_base_url && !cfg.get('baseUrl')) {
    const url = await vscode.window.showInputBox({
      title: 'Base URL of the OpenAI-compatible endpoint',
      placeHolder: 'http://localhost:8080/v1',
      ignoreFocusOut: true,
    });
    if (!url) return;
    await cfg.update('baseUrl', url.trim(), target);
  }
  if (p.needs_key && !p.key_in_env && !(await extensionContext.secrets.get(secretName(p.id)))) {
    if (!(await setApiKey(p.id))) return;
  }
  const model = await vscode.window.showInputBox({
    title: `Model for ${p.label}`,
    prompt: p.default_model
      ? `Leave empty for the default (${p.default_model}).`
      : 'Type the model name exactly as the provider spells it.',
    value: p.id === current ? (cfg.get('model') || '') : '',
    placeHolder: p.model_hint ? `for example: ${p.model_hint}` : undefined,
    ignoreFocusOut: true,
  });
  if (model === undefined) return;
  if (!model.trim() && !p.default_model && p.kind !== 'codex') {
    vscode.window.showWarningMessage(`Vocaret: ${p.label} needs a model name; nothing was changed.`);
    return;
  }
  await cfg.update('provider', p.id, target);
  await cfg.update('model', model.trim(), target);
  vscode.window.showInformationMessage(
    `Vocaret now uses ${p.label}${model.trim() || p.default_model ? ` (${model.trim() || p.default_model})` : ''}.`);
}

// ---------------------------------------------------------------- status UI

function setState(next, detail) {
  state = next;
  vscode.commands.executeCommand('setContext', 'vocaret.recording', next === 'recording');
  if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
  statusItem.backgroundColor = undefined;
  statusItem.command = 'vocaret.toggle';
  if (next === 'idle') {
    statusItem.text = detail ? `$(check) ${detail}` : '$(mic) Vocaret';
    statusItem.tooltip = `Speak a code instruction (Ctrl+Alt+Space). Provider: ${vscode.workspace.getConfiguration('vocaret').get('provider')}`;
    if (detail) idleTimer = setTimeout(() => setState('idle'), 4000);
  } else if (next === 'starting') {
    statusItem.text = '$(loading~spin) Opening microphone...';
  } else if (next === 'recording') {
    statusItem.text = `$(record) Listening ${detail || ''}`;
    statusItem.tooltip = 'Ctrl+Alt+Space to finish, Esc to cancel';
    statusItem.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
  } else if (next === 'working') {
    statusItem.text = `$(loading~spin) ${detail || 'Working...'}`;
    statusItem.tooltip = 'Click to cancel';
    statusItem.command = 'vocaret.cancel';
  }
}

function meter(level) {
  const bars = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
  return bars[Math.max(0, Math.min(bars.length - 1, Math.round(level * (bars.length - 1))))];
}

function shorten(text, max) {
  return text.length > max ? text.slice(0, max - 1) + '…' : text;
}

function fail(err) {
  const message = err && err.message ? err.message : String(err);
  log.appendLine(`error: ${message}`);
  setState('idle');
  vscode.window.showErrorMessage(`Vocaret: ${message}`);
}

// ------------------------------------------------------------ editor context

function usableEditor() {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.document.uri.scheme === 'output') return null;
  return editor;
}

function contextOf(editor) {
  const doc = editor.document;
  const sel = editor.selection;
  const opts = editor.options;
  return {
    path: vscode.workspace.asRelativePath(doc.uri, false),
    language: doc.languageId,
    indent: opts.insertSpaces === false ? 'tabs' : `${opts.tabSize || 4} spaces`,
    text: doc.getText(),
    selection: {
      start: { line: sel.start.line, character: sel.start.character },
      end: { line: sel.end.line, character: sel.end.character },
    },
  };
}

// --------------------------------------------------------------- the flow

async function toggle() {
  if (state === 'idle') return begin();
  if (state === 'recording') return finish();
  // starting / working: a second press shouldn't queue anything up.
}

async function begin() {
  const editor = usableEditor();
  if (!editor) {
    vscode.window.showInformationMessage('Vocaret: put the caret in a file first.');
    return;
  }
  session = { editor, document: editor.document };
  setState('starting');
  try {
    await ensureDaemon();
  } catch (err) {
    return fail(err);
  }
  let autoFinish = false;
  const done = request({ op: 'start', options: await options() }, ev => {
    if (ev.event === 'recording') {
      if (state === 'starting') setState('recording');
    } else if (ev.event === 'level') {
      if (state === 'recording') statusItem.text = `$(record) Listening ${meter(ev.value)}`;
    } else if (ev.event === 'stopped') {
      if (state === 'recording' && (ev.reason === 'silence' || ev.reason === 'max')) {
        autoFinish = true;
      } else if (ev.reason === 'error' && (state === 'recording' || state === 'starting')) {
        fail(new Error('The microphone stopped unexpectedly.'));
      }
    } else if (ev.event === 'error') {
      fail(new Error(ev.message));
    }
  }, socket => { startSocket = socket; });
  done.then(() => {
    startSocket = null;
    if (autoFinish && state === 'recording') finish();
  }, err => {
    startSocket = null;
    if (state === 'starting' || state === 'recording') fail(err);
  });
}

async function finish() {
  if (state !== 'recording' || !session) return;
  setState('working', 'Transcribing...');
  await runAndApply({ op: 'finish' }, session, 0);
}

/** Send a finish/instruct request for the session's document and apply the answer. */
async function runAndApply(base, target, attempt) {
  const { editor, document } = target;
  const version = document.version;
  let result = null;
  let error = null;
  try {
    await request({ ...base, context: contextOf(editor), options: await options() }, ev => {
      if (ev.event === 'transcribing') {
        setState('working', 'Transcribing...');
      } else if (ev.event === 'transcript') {
        log.appendLine(`heard (${ev.stt_ms} ms): ${ev.text}`);
        if (ev.text) setState('working', shorten(ev.text, 60));
      } else if (ev.event === 'generating') {
        if (base.op === 'instruct') setState('working', shorten(base.text, 60));
      } else if (ev.event === 'result') {
        result = ev;
      } else if (ev.event === 'error') {
        error = ev.message;
      }
    });
  } catch (err) {
    return fail(err);
  }
  if (state !== 'working') return false; // cancelled meanwhile
  if (error) {
    if (error === 'Cancelled.') { setState('idle'); return false; }
    return fail(new Error(error));
  }
  if (!result) return fail(new Error('The background service closed the connection.'));

  if (result.llm_ms !== undefined) log.appendLine(`model (${result.llm_ms} ms):\n${result.reply}`);
  if (document.version !== version && result.edits.length) {
    // The programmer kept typing while we worked, so the positions are stale.
    // Redo the instruction against the current text (no need to speak again).
    if (attempt < 2 && result.transcript) {
      log.appendLine('document changed while working; redoing against the new text');
      return runAndApply({ op: 'instruct', text: result.transcript }, target, attempt + 1);
    }
    return fail(new Error('The file kept changing, so the edit was not applied. Try again.'));
  }
  try {
    return await applyResult(result, target);
  } catch (err) {
    return fail(err);
  }
}

async function applyResult(result, { editor, document }) {
  if (result.undo) {
    const mine = lastVoiceEdit && lastVoiceEdit.uri === document.uri.toString()
      && lastVoiceEdit.version === document.version;
    if (mine) {
      await vscode.window.showTextDocument(document, editor.viewColumn);
      await vscode.commands.executeCommand('undo');
      lastVoiceEdit = null;
      setState('idle', 'Undone');
    } else {
      setState('idle');
      vscode.window.showInformationMessage(
        "Vocaret: you've edited since the last voice change, so use Ctrl+Z to undo.");
    }
    return true;
  }
  if (!result.edits.length) {
    setState('idle');
    vscode.window.showInformationMessage(
      `Vocaret: ${result.note || 'Nothing to change.'}`
      + (result.transcript ? `  (heard: "${shorten(result.transcript, 80)}")` : ''));
    return true;
  }

  const eol = document.eol === vscode.EndOfLine.CRLF ? '\r\n' : '\n';
  const workspaceEdit = new vscode.WorkspaceEdit();
  const changed = []; // offsets of the new text in the document after the edit
  let delta = 0;
  for (const edit of result.edits) {
    const range = new vscode.Range(edit.start.line, edit.start.character,
                                   edit.end.line, edit.end.character);
    const text = edit.text.replace(/\n/g, eol);
    const from = document.offsetAt(range.start);
    const to = document.offsetAt(range.end);
    workspaceEdit.replace(document.uri, range, text);
    changed.push([from + delta, from + delta + text.length]);
    delta += text.length - (to - from);
  }
  const applied = await vscode.workspace.applyEdit(workspaceEdit);
  if (!applied) throw new Error('The editor rejected the edit.');
  lastVoiceEdit = { uri: document.uri.toString(), version: document.version };

  const visible = vscode.window.visibleTextEditors.find(e => e.document === document);
  if (visible) {
    if (result.cursor) {
      const caret = new vscode.Position(result.cursor.line, result.cursor.character);
      visible.selection = new vscode.Selection(caret, caret);
      visible.revealRange(new vscode.Range(caret, caret),
                          vscode.TextEditorRevealType.InCenterIfOutsideViewport);
    }
    if (vscode.workspace.getConfiguration('vocaret').get('highlightChanges')) {
      const ranges = changed.filter(([a, b]) => b > a)
        .map(([a, b]) => new vscode.Range(document.positionAt(a), document.positionAt(b)));
      visible.setDecorations(flash, ranges);
      setTimeout(() => visible.setDecorations(flash, []), 1500);
    }
  }
  setState('idle', result.note ? shorten(result.note, 50) : 'Done');
  return true;
}

async function cancel() {
  if (state === 'idle') return;
  const wasStarting = state === 'starting';
  setState('idle');
  if (startSocket) { startSocket.destroy(); startSocket = null; }
  if (!wasStarting) {
    try { await request({ op: 'cancel' }, () => {}); } catch (err) { /* daemon gone */ }
  }
}

async function instruct(textArg) {
  if (state !== 'idle') return false;
  const editor = usableEditor();
  if (!editor) {
    vscode.window.showInformationMessage('Vocaret: put the caret in a file first.');
    return false;
  }
  const text = typeof textArg === 'string' ? textArg : await vscode.window.showInputBox({
    prompt: 'What should be typed? (same as speaking it)',
    placeHolder: 'create a public method called test that takes two strings and returns them joined',
  });
  if (!text || !text.trim()) return false;
  setState('working', shorten(text, 60));
  try {
    await ensureDaemon();
  } catch (err) {
    return fail(err);
  }
  return runAndApply({ op: 'instruct', text }, { editor, document: editor.document }, 0);
}

async function restartDaemon() {
  await cancel();
  await stopDaemon();
  try {
    await ensureDaemon();
    vscode.window.showInformationMessage('Vocaret: background service restarted.');
  } catch (err) {
    fail(err);
  }
}

async function setup() {
  try {
    await cancel();
    await runSetup();
    await ensureDaemon();
    vscode.window.showInformationMessage('Vocaret: the speech engine is installed and up to date.');
  } catch (err) {
    fail(err);
  }
}

async function doctor() {
  const launcher = launcherPath();
  if (!launcher) {
    vscode.window.showInformationMessage('Vocaret: run "Vocaret: Install or Update Speech Engine" first.');
    return;
  }
  const cfg = vscode.workspace.getConfiguration('vocaret');
  const quote = text => `'${String(text).replace(/'/g, `'\\''`)}'`;
  let command = `${quote(launcher)} doctor --provider ${quote(cfg.get('provider'))}`;
  if (cfg.get('model')) command += ` --model ${quote(cfg.get('model'))}`;
  const env = {};
  const opts = await options();
  // Hand the stored key to the check through the environment, not the command line.
  if (opts.api_key) env.VOCARET_API_KEY = opts.api_key;
  const terminal = vscode.window.createTerminal({ name: 'Vocaret setup check', env });
  terminal.show();
  terminal.sendText(command);
}

// ---------------------------------------------------------------- lifecycle

function activate(context) {
  extensionContext = context;
  log = vscode.window.createOutputChannel('Vocaret');
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 1000);
  flash = vscode.window.createTextEditorDecorationType({
    backgroundColor: new vscode.ThemeColor('diffEditor.insertedTextBackground'),
  });
  setState('idle');
  statusItem.show();
  const register = (name, fn) =>
    context.subscriptions.push(vscode.commands.registerCommand(name, fn));
  register('vocaret.toggle', toggle);
  register('vocaret.cancel', cancel);
  register('vocaret.instruct', instruct);
  register('vocaret.selectProvider', selectProvider);
  register('vocaret.setApiKey', setApiKey);
  register('vocaret.clearApiKey', clearApiKey);
  register('vocaret.setup', setup);
  register('vocaret.showLog', () => log.show());
  register('vocaret.doctor', doctor);
  register('vocaret.restartDaemon', restartDaemon);
  context.subscriptions.push(statusItem, log, flash);
  // Exposed for the integration test.
  return { getState: () => state, socketPath };
}

function deactivate() {
  // Closing the "start" connection makes the daemon drop the recording.
  if (startSocket) startSocket.destroy();
}

module.exports = { activate, deactivate };
