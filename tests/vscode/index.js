// Integration test that runs inside a real VS Code extension host.
// Launched by tests/run_vscode_test.sh; writes its report to $VOCARET_TEST_REPORT.
const vscode = require('vscode');
const fs = require('fs');
const assert = require('assert');

const sleep = ms => new Promise(r => setTimeout(r, ms));
const report = { passed: [], failed: [] };

async function step(name, fn) {
  const started = Date.now();
  try {
    await fn();
    report.passed.push(`${name} (${((Date.now() - started) / 1000).toFixed(1)}s)`);
  } catch (err) {
    report.failed.push(`${name}: ${err && err.message}`);
  }
}

async function waitFor(predicate, what, timeout = 60000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await sleep(100);
  }
  throw new Error(`timed out waiting for ${what}`);
}

async function open(language, content) {
  const doc = await vscode.workspace.openTextDocument({ language, content });
  const editor = await vscode.window.showTextDocument(doc);
  return { doc, editor };
}

const say = text => vscode.commands.executeCommand('vocaret.instruct', text);

exports.run = async function run() {
  const extension = vscode.extensions.getExtension('vocaret.vocaret');
  const api = await extension.activate();
  const cfg = vscode.workspace.getConfiguration('vocaret');
  await cfg.update('command', process.env.VOCARET_TEST_LAUNCHER, vscode.ConfigurationTarget.Global);

  let java;
  await step('typed instruction writes a method into an empty Java file', async () => {
    java = await open('java', '');
    await say('create a public method called test with parameters of string x and string y, and return x plus y');
    assert.match(java.doc.getText(), /public String test\(String x, String y\)\s*\{\s*return x \+ y;\s*\}/);
  });
  let beforeWrap;
  await step('follow-up restructures it ("put that inside a class Foo")', async () => {
    beforeWrap = java.doc.getText();
    await say('put that inside a class of foo, and have a constructor for foo that accepts an argument string called s and prints out s');
    const text = java.doc.getText();
    assert.match(text, /class Foo\s*\{/);
    assert.match(text, /Foo\(String s\)\s*\{\s*System\.out\.println\(s\);/);
    assert.match(text, /    public String test\(String x, String y\)/);
    report.java = text;
  });
  await step('"undo that" reverts the last voice edit in one step', async () => {
    await say('undo that');
    assert.strictEqual(java.doc.getText(), beforeWrap);
  });

  await step('CRLF file, caret mid-file, existing code left untouched', async () => {
    const source = 'import os\r\n\r\ndef first():\r\n    return 1\r\n\r\n\r\ndef last():\r\n    return 3\r\n';
    const { doc, editor } = await open('python', source);
    await editor.edit(b => b.setEndOfLine(vscode.EndOfLine.CRLF));
    const caret = new vscode.Position(5, 0);
    editor.selection = new vscode.Selection(caret, caret);
    await say('define a function called second that returns two');
    const text = doc.getText();
    assert.match(text, /def second\(\):\r\n    return 2/);
    assert.ok(!/[^\r]\n/.test(text), 'line endings stayed CRLF');
    assert.ok(text.startsWith('import os\r\n\r\ndef first():\r\n    return 1\r\n'));
    assert.ok(text.endsWith('def last():\r\n    return 3\r\n'));
    assert.ok(text.indexOf('def first') < text.indexOf('def second')
      && text.indexOf('def second') < text.indexOf('def last'));
    report.python = text;
  });

  await step('typing while Claude works: edit is redone against the new text', async () => {
    const { doc, editor } = await open('javascript', 'const a = 1;\n');
    const end = new vscode.Position(1, 0);
    editor.selection = new vscode.Selection(end, end);
    const pending = say('declare a constant b equal to two');
    await sleep(400);
    await editor.edit(b => b.insert(new vscode.Position(0, 0), '// typed by hand meanwhile\n'));
    await pending;
    const text = doc.getText();
    assert.ok(text.startsWith('// typed by hand meanwhile\nconst a = 1;\n'), text);
    assert.match(text, /const b = 2;/);
  });

  await step('real microphone opens and Esc-cancel releases it', async () => {
    await open('plaintext', '');
    await vscode.commands.executeCommand('vocaret.toggle');
    await waitFor(() => api.getState() === 'recording', 'the microphone to open', 15000);
    await vscode.commands.executeCommand('vocaret.cancel');
    assert.strictEqual(api.getState(), 'idle');
  });

  await step('spoken instruction end to end (audio -> code), auto-finish on silence', async () => {
    const wav = process.env.VOCARET_TEST_WAV;
    await cfg.update('recorderCommand',
      `ffmpeg -v error -re -i ${wav} -af apad=pad_dur=6 -f s16le -ar 16000 -ac 1 -`,
      vscode.ConfigurationTarget.Global);
    await cfg.update('autoStopSilenceSeconds', 1.2, vscode.ConfigurationTarget.Global);
    const { doc } = await open('java', '');
    await vscode.commands.executeCommand('vocaret.toggle');
    await waitFor(() => api.getState() === 'recording', 'recording to start', 15000);
    await waitFor(() => api.getState() === 'idle' && doc.getText().length > 0, 'the code to arrive', 60000);
    assert.match(doc.getText(), /public String test\(String x, String y\)/);
  });

  await step('API-key provider: key from secret storage reaches an OpenAI-compatible server', async () => {
    const http = require('http');
    const calls = [];
    const server = http.createServer((req, res) => {
      let body = '';
      req.on('data', chunk => { body += chunk; });
      req.on('end', () => {
        calls.push({ url: req.url, auth: req.headers.authorization, body: JSON.parse(body) });
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({ choices: [{ message: { role: 'assistant',
          content: '=====INSERT\nanswer = 42\n=====END' } }] }));
      });
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const global = vscode.ConfigurationTarget.Global;
    try {
      await cfg.update('recorderCommand', undefined, global);
      await cfg.update('autoStopSilenceSeconds', undefined, global);
      await cfg.update('provider', 'custom', global);
      await cfg.update('baseUrl', `http://127.0.0.1:${server.address().port}/v1`, global);
      await cfg.update('model', 'stand-in-model', global);
      await vscode.commands.executeCommand('vocaret.setApiKey', 'custom', 'test-key-not-real');
      const { doc } = await open('python', '');
      await say('set answer to forty two');
      assert.strictEqual(doc.getText(), 'answer = 42');
      assert.strictEqual(calls.length, 1);
      assert.strictEqual(calls[0].url, '/v1/chat/completions');
      assert.strictEqual(calls[0].auth, 'Bearer test-key-not-real');
      assert.strictEqual(calls[0].body.model, 'stand-in-model');
      assert.ok(!JSON.stringify(cfg.inspect('provider')).includes('test-key'), 'key is not in settings');
    } finally {
      await cfg.update('provider', undefined, global);
      await cfg.update('baseUrl', undefined, global);
      await cfg.update('model', undefined, global);
      server.close();
    }
  });

  fs.writeFileSync(process.env.VOCARET_TEST_REPORT, JSON.stringify(report, null, 2));
  if (report.failed.length) throw new Error(report.failed.join('\n'));
};
