import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const request = async (path, body) => {
  const response = await api.fetchApi(`/ambient/${path}`, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : {});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
};

app.registerExtension({
  name: "modal.ambient.workflows",
  async setup() {
    let root, timer, records = [], catalog, selected, draft, revision, loadedId;
    let following = false, busy = false;
    const session = document.createElement("select");
    const stage = document.createElement("select");
    const history = document.createElement("select");
    const status = document.createElement("p");
    const inputs = document.createElement("div");
    const results = document.createElement("pre");
    const ports = document.createElement("textarea");
    ports.rows = 7;
    ports.setAttribute("aria-label", "Ambient input and output bindings");
    const report = (error) => { status.textContent = String(error.message || error); };
    const button = (label, action) => {
      const item = document.createElement("button");
      item.textContent = label;
      item.onclick = () => void Promise.resolve().then(action).catch(report);
      return item;
    };
    const choose = () => records.find((record) =>
      record.meta.sessionId === session.value && record.meta.stage === stage.value);
    const load = async (record) => {
      if (!record) return;
      // Runtime input bindings may differ from the saved editor widgets. Load
      // the actual API graph, then restore layout only, never stale values.
      await app.loadApiJson(structuredClone(record.graph), `Ambient ${record.meta.stage}`);
      for (const saved of record.workflow?.nodes || []) {
        const node = app.graph.getNodeById(saved.id);
        if (!node) continue;
        if (saved.pos) node.pos = saved.pos;
        if (saved.size) node.size = saved.size;
        if (saved.title) node.title = saved.title;
      }
      app.nodeOutputs = record.outputs || {};
      loadedId = record.id;
    };
    const showInputs = () => {
      inputs.replaceChildren();
      for (const [name, binding] of Object.entries(draft.bindings)) {
        const label = document.createElement("label");
        label.textContent = `${name} `;
        const source = document.createElement("select");
        for (const [value, text] of [["ambient", "Ambientから入力"], ["workflow", "ワークフローの値"]]) {
          source.add(new Option(text, value));
        }
        source.value = binding.source || "ambient";
        source.onchange = () => { binding.source = source.value; ports.value = JSON.stringify({ bindings: draft.bindings, outputs: draft.outputs }, null, 2); };
        label.append(source);
        inputs.append(label, document.createElement("br"));
      }
      ports.value = JSON.stringify({ bindings: draft.bindings, outputs: draft.outputs }, null, 2);
    };
    const edit = async (template) => {
      selected = choose();
      if (!selected) throw new Error("Ambientでこの処理を一度実行してください。");
      following = false;
      session.disabled = stage.disabled = true;
      draft = structuredClone(template || {
        graph: selected.graph, workflow: selected.workflow,
        bindings: selected.meta.bindings, outputs: selected.meta.outputs,
      });
      revision = catalog.revision;
      await load({ ...selected, ...draft });
      showInputs();
      status.textContent = `編集中 · ${selected.meta.stage} · 基準版 ${revision}`;
    };
    const refresh = async () => {
      if (busy || !root?.isConnected) return;
      busy = true;
      try {
        const [executions, workflows] = await Promise.all([request("executions"), request("workflows")]);
        records = executions.executions;
        catalog = workflows;
        records.push(...Object.entries(catalog.stages).map(([stage, template]) => ({
          id: `template-${stage}-${catalog.revision}`, status: "template", graph: template.graph,
          workflow: template.workflow, outputs: {},
          meta: { sessionId: "templates", stage, revision: catalog.revision,
            bindings: template.bindings, outputs: template.outputs },
        })));
        const fill = (select, values) => {
          const previous = select.value;
          select.replaceChildren(...values.map(([value, label]) => new Option(label, value)));
          if (values.some(([value]) => value === previous)) select.value = previous;
        };
        fill(session, [...new Set(records.map((r) => r.meta.sessionId))].map((id) => [id, id === "templates" ? "ワークフローテンプレート" : `Session ${id.slice(0, 8)}`]));
        fill(stage, [...new Set(records.filter((r) => r.meta.sessionId === session.value).map((r) => r.meta.stage))].map((id) => [id, id]));
        fill(history, [...catalog.history].reverse().map((id) => [String(id), `保存版 ${id}`]));
        const record = choose();
        if (following && !draft && record) {
          if (loadedId !== record.id) await load(record);
          const event = record.event;
          status.textContent = `${record.meta.stage} · ${record.status} · 版 ${record.meta.revision} · ${event?.type || ""} ${event?.data?.node || ""}`;
          app.nodeOutputs = record.outputs || {};
          results.textContent = JSON.stringify(record.outputs || {}, null, 2);
        }
      } catch (error) { if (!draft) report(error); }
      finally { busy = false; }
    };
    api.addEventListener("ambient_execution", (event) => {
      const data = event.detail;
      if (!following || draft || data.sessionId !== session.value || data.stage !== stage.value ||
          data.event?.data?.prompt_id !== loadedId) return;
      status.textContent = `${stage.value} · ${data.event.type} · ${JSON.stringify(data.event.data)}`;
      // Only the graph explicitly being followed receives native progress events.
      api.dispatchEvent(new CustomEvent(data.event.type, { detail: data.event.data }));
    });
    app.extensionManager.registerSidebarTab({
      id: "ambient", icon: "pi pi-video", title: "Ambient", tooltip: "Ambientの実行内容とワークフロー", type: "custom",
      render(container) {
        root = container;
        const title = document.createElement("h3"); title.textContent = "Ambient";
        const details = document.createElement("details");
        const summary = document.createElement("summary"); summary.textContent = "入出力の接続先（ノードを置き換えた場合）";
        details.append(summary, ports);
        container.replaceChildren(title, session, stage,
          button("実行を追従表示", async () => {
            if (draft) throw new Error("編集を適用するか、破棄してから追従してください。");
            following = true; loadedId = null; await refresh();
          }),
          button("編集用に開く", () => edit()),
          button("編集を破棄", () => { draft = null; session.disabled = stage.disabled = false; inputs.replaceChildren(); status.textContent = "編集を破棄しました。"; }),
          inputs, details,
          button("Ambientに適用", async () => {
            if (!draft) throw new Error("先に編集用に開いてください。");
            const exported = await app.graphToPrompt();
            const bindings = JSON.parse(ports.value);
            const template = { graph: exported.output, workflow: exported.workflow, ...bindings };
            await request("workflows/validate", { stage: selected.meta.stage, template });
            catalog = await request("workflows/apply", { stage: selected.meta.stage, template, expectedRevision: revision });
            draft = null; session.disabled = stage.disabled = false; inputs.replaceChildren();
            status.textContent = `版 ${catalog.revision} を保存しました。次の生成サイクルから使用します。`;
          }),
          history,
          button("保存版を編集用に復元", async () => {
            const old = await request(`workflows/${history.value}`);
            const template = old.stages[stage.value];
            if (!template) throw new Error("この版には編集済みワークフローがありません。");
            await edit(template);
          }), status, results);
        container.style.cssText = "padding:12px;display:flex;flex-direction:column;gap:10px;overflow:auto";
        results.style.cssText = "white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px";
        session.setAttribute("aria-label", "Ambient session"); stage.setAttribute("aria-label", "Processing stage");
        session.onchange = stage.onchange = () => { if (!draft) { loadedId = null; void refresh(); } };
        clearInterval(timer); timer = setInterval(refresh, 3000); void refresh();
      },
    });
  },
});
