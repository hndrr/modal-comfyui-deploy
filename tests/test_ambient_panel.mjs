import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
import assert from 'node:assert/strict';

// Exercise the extension against the public app/api surface without a ComfyUI
// process or any generation provider. Graph serialization remains the app's job.
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.value = ''; this.style = {};
    this.isConnected = true; this.disabled = false; this.textContent = '';
  }
  append(...items) { this.children.push(...items); }
  add(item) { this.append(item); if (!this.value) this.value = item.value; }
  replaceChildren(...items) {
    this.children = items;
    if (this.tag === 'select') this.value = items[0]?.value || '';
  }
  setAttribute(name, value) { this[name] = value; }
}
class Option extends Element {
  constructor(text, value) { super('option'); this.textContent = text; this.value = value; }
}
class CustomEvent extends Event {
  constructor(type, { detail }) { super(type); this.detail = detail; }
}
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(setImmediate); };

test('mirror correlates events and protects a draft across refresh, conflict and restore', async () => {
  const template = {
    graph: { '1': { class_type: 'Text', inputs: { prompt: 'initial' } } },
    workflow: { nodes: [{ id: 1, pos: [2, 4], widgets_values: ['stale value'] }] },
    bindings: { prompt: { node: '1', input: 'prompt', source: 'ambient' } }, outputs: { text: '1' },
  };
  let catalog = { revision: 0, history: [0], stages: { text: template } };
  const record = (id, sessionId, prompt) => ({
    id, status: 'running', graph: { '1': { class_type: 'Text', inputs: { prompt } } },
    workflow: template.workflow, outputs: {},
    meta: { sessionId, stage: 'text', revision: 0, bindings: template.bindings, outputs: template.outputs },
  });
  let records = [record('a1', 'session-a', 'actual submitted value'), record('b1', 'session-b', 'other session')];
  const api = new EventTarget(), requests = [], loads = [], nativeEvents = [];
  let extension, sidebar, refresh, exported, applied;
  const node = { id: 1 };
  const app = {
    registerExtension(value) { extension = value; },
    extensionManager: { registerSidebarTab(value) { sidebar = value; } },
    graph: { getNodeById() { return node; } },
    async loadApiJson(graph) { loads.push(graph); exported = graph; },
    async graphToPrompt() { return { output: exported, workflow: template.workflow }; },
  };
  api.fetchApi = async (path, options) => {
    requests.push(path);
    let result, status = 200;
    if (path === '/ambient/executions') result = { executions: structuredClone(records) };
    else if (path === '/ambient/workflows') result = catalog;
    else if (path === '/ambient/workflows/0') result = { revision: 0, stages: { text: template } };
    else if (path.endsWith('/validate')) result = { valid: true };
    else if (path.endsWith('/apply')) {
      const body = JSON.parse(options.body);
      if (body.expectedRevision !== catalog.revision) {
        status = 409; result = { error: 'Workflow changed on another device.' };
      } else {
        applied = body;
        result = catalog = { revision: catalog.revision + 1, history: [0, 1, 2], stages: { text: body.template } };
      }
    } else throw new Error(`Unexpected request: ${path}`);
    return new Response(JSON.stringify(result), { status });
  };
  api.addEventListener('progress', (event) => nativeEvents.push(event.detail));
  const source = readFileSync(new URL('../extensions/ComfyUI-Modal-Control/web/ambient.js', import.meta.url), 'utf8')
    .replace(/^import .*;\n/gm, '');
  runInNewContext(source, {
    app, api, document: { createElement: (tag) => new Element(tag) }, Option, CustomEvent,
    structuredClone, setInterval: (callback) => { refresh = callback; return 1; }, clearInterval() {},
  });
  await extension.setup();
  const root = new Element('div'); sidebar.render(root); await settle();
  const click = async (text) => {
    const item = root.children.find((child) => child.textContent === text);
    assert.ok(item, text); item.onclick(); await settle();
  };
  await click('実行を追従表示');
  assert.equal(exported['1'].inputs.prompt, 'actual submitted value');
  assert.deepEqual(Array.from(node.pos), [2, 4]);
  const progress = (id, sessionId) => api.dispatchEvent(new CustomEvent('ambient_execution', {
    detail: { sessionId, stage: 'text', event: { type: 'progress', data: { prompt_id: id, node: '1', value: 3 } } },
  }));
  progress('b1', 'session-b'); assert.equal(nativeEvents.length, 0);
  progress('a1', 'session-a'); assert.equal(nativeEvents.length, 1);
  await click('編集用に開く');
  exported['1'].inputs.prompt = 'unsaved draft';
  records = [record('a2', 'session-a', 'next execution'), ...records];
  catalog = { ...catalog, revision: 1, history: [0, 1] };
  const count = loads.length; await refresh();
  assert.equal(loads.length, count);
  assert.equal(exported['1'].inputs.prompt, 'unsaved draft');
  progress('a1', 'session-a'); assert.equal(nativeEvents.length, 1);
  await click('Ambientに適用');
  assert.equal(applied, undefined);
  assert.equal(exported['1'].inputs.prompt, 'unsaved draft');
  assert.equal(root.children.find((child) => child['aria-label'] === 'Ambient session').disabled, true);
  const history = root.children.filter((child) => child.tag === 'select').at(-1);
  history.value = '0'; await click('保存版を編集用に復元');
  assert.equal(exported['1'].inputs.prompt, 'initial');
  await click('Ambientに適用');
  assert.equal(applied.expectedRevision, 1);
  assert.equal(applied.template.graph['1'].inputs.prompt, 'initial');
  assert.ok(requests.every((path) => !path.includes('/prompt')));
});
