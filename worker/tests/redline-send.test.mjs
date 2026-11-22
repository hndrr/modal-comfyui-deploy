import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";
import {runInNewContext} from "node:vm";

const script = await readFile(new URL("../viewer/redline.js", import.meta.url), "utf8");
const video = {prompt_id: "job", index: 0, url: "/clip.mp4", filename: "clip.mp4", etag: '"v1"'};
const key = JSON.stringify([video.prompt_id, video.index, video.url, video.etag]);
const mark = {
  id: "11111111-1111-4111-8111-111111111111", frame: 0, start: 0, end: 0,
  note: "remove sign", shapes: [], image: "data:image/jpeg;base64,/9j/2Q==",
};

function openWidget(host, fetch) {
  const element = () => ({
    addEventListener() {}, append() {}, before() {}, replaceChildren() {}, setAttribute() {},
    classList: {toggle() {}}, getContext: () => ({setTransform() {}, clearRect() {}}),
  });
  const elements = new Map();
  const document = {createElement: element, getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }};
  const render = runInNewContext(script + "\nreviewResult", {
    window: {openai: host}, document, player: element(), initialized: false, serverTools: false,
    devicePixelRatio: 1, fetch, URL, AbortSignal,
    addEventListener() {}, ResizeObserver: class { observe() {} }, File, atob,
  });
  render(video, {});
  return {render, send: elements.get("rl-send"), openShare: elements.get("rl-open"),
    sync: elements.get("rl-sync"), status: elements.get("rl-status")};
}

test("switching videos during a shared save preserves a later deletion in the admin draft", async () => {
  const meta = {"split/review": {sharing: true, version: video.etag}};
  const host = {
    widgetState: {privateContent: {redline: {[key]: {marks: [mark]}}}},
    setWidgetState(state) { this.widgetState = structuredClone(state); },
    async callTool() {
      return {_meta: {"pmd/review": {base: "https://media.example.test/draft", label: video.filename,
        version: video.etag, ref: {prompt_id: video.prompt_id, index: 0}}}};
    },
  };
  let posts = 0;
  let remote = {revision: 0, marks: []};
  const fetchDraft = async (_url, init) => {
    if (init.method !== "POST") return Response.json(remote);
    posts++;
    remote = {revision: remote.revision + 1, marks: JSON.parse(init.body).marks};
    widget.render({...video, etag: '"v2"'}, {});
    return Response.json({revision: remote.revision});
  };
  const widget = openWidget(host, fetchDraft);
  widget.render(video, meta);
  await widget.openShare.onclick();
  await widget.sync.onclick();
  assert.deepEqual(remote.marks.map(m => m.id), [mark.id]);
  assert.deepEqual(host.widgetState.privateContent.redline[key].known, [mark.id]);
  assert.equal(widget.status.textContent, "");

  remote = {revision: remote.revision + 1, marks: []};
  const reopened = openWidget(host, fetchDraft);
  reopened.render(video, meta);
  await reopened.openShare.onclick();
  await reopened.sync.onclick();
  assert.equal(posts, 1);
  assert.deepEqual(host.widgetState.privateContent.redline[key].marks, []);
});

for (const interruption of ["video switch", "state save rejection"]) {
  test(`native send restores the previous widget context after ${interruption}`, async () => {
    const prior = interruption === "video switch" ? {modelContent: "previous message", imageIds: ["previous-file"]} : {};
    const messages = [];
    let interrupt = true;
    const host = {
      widgetState: {...prior, privateContent: {redline: {[key]: {marks: [mark]}}}},
      uploadFile: async () => ({fileId: "new-file"}),
      async setWidgetState(state) {
        if (interrupt && state.imageIds?.includes("new-file")) {
          interrupt = false;
          if (interruption === "state save rejection") throw Error("Save failed");
          widget.render({...video, etag: '"v2"'}, {});
        }
        this.widgetState = structuredClone(state);
      },
      async sendFollowUpMessage({prompt}) { messages.push(prompt); },
    };
    const widget = openWidget(host);
    await widget.send.onclick();
    assert.equal(interrupt, false);
    assert.deepEqual(messages, []);
    const {privateContent, ...context} = host.widgetState;
    assert.deepEqual(context, prior);
    assert.equal(privateContent.redline[key].marks[0].note, mark.note);
    assert.deepEqual(privateContent.redline[key].sent, {});

    widget.render(video, {});
    assert.equal(widget.send.disabled, false);
    await widget.send.onclick();
    assert.equal(messages.length, 1);
    assert.match(messages[0], /remove sign/);
    assert.equal(host.widgetState.modelContent, messages[0]);
    assert.deepEqual(host.widgetState.imageIds, ["new-file"]);
  });
}
