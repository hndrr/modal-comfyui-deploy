import { app, registerGpuSidebar, registerGpuIndicator, saveWorkflow, restoreWorkflow } from "./comfy-adapter.mjs";

const restoreKey = "modal-split-workflow";
const request = async (path, body) => {
  const url = path === "status" ? "/modal-control/v1/status" : `/split/${path}`;
  const response = await fetch(url, body === undefined ? { cache: "no-store", signal: AbortSignal.timeout(8000) } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  if (path === "status" && result.api_version !== 1) throw new Error("非対応のModal Control APIです。");
  return result;
};

app.registerExtension({
  name: "Modal.SplitExecution",
  async setup() {
    // A regular ComfyUI installation without the Modal backend stays untouched.
    try { await request("status"); } catch (e) {
      console.info("Modal Control is unavailable:", e.message);
      return;
    }
    const panel = document.createElement("section");
    panel.className = "modal-control-panel";
    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = new URL("./panel.css", import.meta.url).href;
    document.head.append(stylesheet);
    const heading = document.createElement("h2");
    heading.textContent = "GPU";
    const button = document.createElement("div");
    button.className = "modal-gpu-state";
    button.textContent = "確認中";
    button.setAttribute("role", "status");
    const description = document.createElement("p");
    description.className = "modal-hint";
    const gpuDescription = document.createElement("p");
    gpuDescription.setAttribute("role", "status");
    let updateIndicator = () => {};
    const showGpu = (gpu) => {
      const labels = { stopped: "停止中", starting: "起動待ち", active: "使用中",
        stopping: "停止待ち", legacy: "常駐中", maintenance: "環境検証中", unknown: "確認できません" };
      const known = Number.isInteger(gpu?.containers) && gpu?.checked_at;
      const label = known ? (labels[gpu.phase] || "確認できません") : labels.unknown;
      const count = known ? gpu.containers : "?";
      button.textContent = `GPU(${count})`;
      button.dataset.state = !known ? "unknown" : gpu.containers === 0 ? "off" : "on";
      const detail = !known ? "状態を取得できません。GPUが停止したかは不明です。"
        : gpu.containers === 0 ? "GPU料金は発生していません。画面を開いたままで大丈夫です。"
        : gpu.phase === "stopping" ? "GPU料金が発生しています。自動停止を待っています（30秒以上かかる場合があります）。"
        : "GPU料金が発生しています。";
      gpuDescription.textContent = `${detail}${known ? ` 最終確認：${new Date(gpu.checked_at * 1000).toLocaleTimeString()}` : ""}`;
      button.title = gpuDescription.textContent;
      updateIndicator(count, `${label}：${gpuDescription.textContent}`, button.dataset.state);
    };
    const error = document.createElement("p");
    error.style.cssText = "color:#ffb6a8;white-space:pre-wrap;max-height:240px;overflow:auto";
    const controls = document.createElement("div");
    controls.className = "modal-controls";
    const advanced = document.createElement("section");
    advanced.className = "modal-settings";
    const settingsHint = document.createElement("p");
    settingsHint.className = "modal-hint";
    settingsHint.textContent = "設定は全員に適用されます。通常は変更不要です。";
    advanced.append(settingsHint, controls);
    let working = false;
    const action = (label, handler) => {
      const item = document.createElement("button"); item.textContent = label;
      item.onclick = async () => {
        working = true; item.disabled = true; error.textContent = "";
        try { await handler(); } catch (e) { error.textContent = e.message; }
        finally { working = false; await refresh().catch(() => showGpu(null)); }
      };
      controls.append(item); return item;
    };
    let state;
    const refresh = async () => {
      state = await request("status");
      showGpu(state.gpu);
      description.textContent = state.mode === "legacy"
        ? "常時起動：生成していない間もGPUを使います。"
        : "生成時だけ起動：生成が終わると自動で停止します。CPU・ストレージ等の料金は別途発生します。";
      mode.textContent = state.mode === "split" ? "GPUを常時起動にする" : "生成時だけ起動に戻す";
      const blocked = working || state.busy || state.transitioning;
      mode.disabled = blocked || Boolean(state.candidate);
      mode.title = mode.disabled ? "生成・更新・切替の完了を待ってください" : "ワークフローを保存してから切り替えます";
      updates.hidden = !state.candidate;
      apply.disabled = blocked || state.candidate?.status !== "editing";
      discard.disabled = blocked || !["editing", "failed"].includes(state.candidate?.status);
      const updateLabels = {creating:"更新を準備中", editing:"未反映のノード更新があります", validating:"更新を検証中", failed:"更新に失敗しました"};
      updateText.textContent = updateLabels[state.candidate?.status] || "";
      if (state.candidate?.error) error.textContent = state.candidate.error;
      if (state.unknown_jobs.length) error.textContent = "状態を確認できないジョブがあります。再実行せず、管理者が実行記録を確認してください。";
    };
    const save = async () => {
      const graph = await saveWorkflow();
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
    const updates = document.createElement("section");
    updates.hidden = true;
    const updateText = document.createElement("p");
    updates.append(updateText);
    const apply = action("ノード更新を反映", async () => {
      await request("environment/apply", {}); await refresh();
    });
    const discard = action("未反映の変更を取り消す", async () => {
      await request("environment/discard", {}); await refresh();
    });
    updates.append(apply, discard);
    advanced.append(updates);
    panel.append(heading, button, gpuDescription, description, error, advanced);
    registerGpuSidebar((element) => element.replaceChildren(panel));
    updateIndicator = registerGpuIndicator();
    const poll = async () => {
      try { await refresh(); } catch { showGpu(null); }
      setTimeout(poll, 5000);
    };
    void poll();
    const saved = sessionStorage.getItem(restoreKey);
    if (saved) {
      await restoreWorkflow(JSON.parse(saved));
      sessionStorage.removeItem(restoreKey);
    }
  }
});
