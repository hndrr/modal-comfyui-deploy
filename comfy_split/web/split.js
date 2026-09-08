import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const restoreKey = "modal-split-workflow";
const request = async (path, body) => {
  const response = await fetch(`/split/${path}`, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
};

app.registerExtension({
  name: "Modal.SplitExecution",
  async setup() {
    const button = document.createElement("button");
    button.textContent = "実行環境";
    button.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:10000;padding:8px 14px;border-radius:8px;border:1px solid #777;background:#222;color:white;cursor:pointer";
    const dialog = document.createElement("dialog");
    dialog.style.cssText = "max-width:440px;padding:24px;background:#252525;color:#eee;border:1px solid #666;border-radius:12px";
    const title = document.createElement("h3");
    title.textContent = "実行環境";
    const description = document.createElement("p");
    const error = document.createElement("p");
    error.style.cssText = "color:#ffb6a8;white-space:pre-wrap;max-height:240px;overflow:auto";
    const controls = document.createElement("div");
    controls.style.cssText = "display:flex;gap:8px;flex-wrap:wrap";
    const action = (label, handler) => {
      const item = document.createElement("button"); item.textContent = label;
      item.onclick = async () => {
        item.disabled = true; error.textContent = "";
        try { await handler(); } catch (e) { error.textContent = e.message; }
        finally { item.disabled = false; }
      };
      controls.append(item); return item;
    };
    let state;
    const refresh = async () => {
      state = await request("status");
      description.textContent = state.mode === "legacy"
        ? "従来モード：画面を使っている間もGPU待機料金が発生します。"
        : "分離モード：編集・保存・閲覧はGPUを使いません。生成後30秒でGPUが停止対象になります。";
      mode.textContent = state.mode === "split" ? "従来モードに切替" : "分離モードに切替";
      if (state.candidate) description.textContent += `\n環境更新：${state.candidate.status}`;
      if (state.candidate?.error) error.textContent = state.candidate.error;
      if (state.unknown_jobs.length) error.textContent = "状態を確認できないジョブがあります。再実行せず、管理者が実行記録を確認してください。";
    };
    const save = async () => {
      const graph = app.graph.serialize();
      const file = `workflows/modal-mode-switch-${Date.now()}.json`;
      await api.storeUserData(file, graph);
      sessionStorage.setItem(restoreKey, JSON.stringify(graph));
    };
    const waitForMode = async (desired) => {
      const deadline = Date.now() + 360000;
      while (Date.now() < deadline) {
        const current = await request("status");
        if (current.mode === desired && !current.transitioning) { location.reload(); return; }
        description.textContent = "GPUの起動・モード切替を待っています…";
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      throw new Error("切替がまだ完了していません。状態を更新して確認してください。");
    };
    const mode = action("モード切替", async () => {
      await refresh(); await save();
      const desired = state.mode === "split" ? "legacy" : "split";
      await request("mode", { mode: desired }); await waitForMode(desired);
    });
    action("ノード更新を検証・反映", async () => {
      await request("environment/apply", {}); await refresh();
    });
    action("未反映の更新を破棄", async () => {
      await request("environment/discard", {}); await refresh();
    });
    action("状態を更新", refresh);
    action("閉じる", () => dialog.close());
    dialog.append(title, description, error, controls);
    document.body.append(button, dialog);
    button.onclick = async () => { dialog.showModal(); try { await refresh(); } catch (e) { error.textContent = e.message; } };
    const saved = sessionStorage.getItem(restoreKey);
    if (saved) {
      await app.loadGraphData(JSON.parse(saved));
      sessionStorage.removeItem(restoreKey);
    }
  }
});
