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

function openWidget(host) {
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
    window: {openai: host}, document, player: element(), initialized: false, devicePixelRatio: 1,
    addEventListener() {}, ResizeObserver: class { observe() {} }, File, atob,
  });
  render(video, {});
  return {render, send: elements.get("rl-send")};
}

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
