// Keep ComfyUI-specific workflow APIs in one place for compatibility updates.
import { app } from "../../scripts/app.js";
import { ComfyButton } from "../../scripts/ui/components/button.js";
import { api } from "../../scripts/api.js";
export { app };
export async function saveWorkflow() {
  if (!app.graph?.serialize || !api.storeUserData) {
    throw new Error("このComfyUIではワークフロー保存APIを利用できません。モード切替を中止しました。");
  }
  const graph = app.graph.serialize();
  await api.storeUserData(`workflows/modal-mode-switch-${Date.now()}.json`, graph);
  return graph;
}
export async function restoreWorkflow(graph) {
  if (!app.loadGraphData) throw new Error("ワークフロー復元APIを利用できません。");
  await app.loadGraphData(graph);
}

export function registerGpuSidebar(render) {
  if (!app.extensionManager?.registerSidebarTab) {
    throw new Error("GPU表示にはサイドバー拡張APIに対応したComfyUIが必要です。");
  }
  app.extensionManager.registerSidebarTab({
    id: "modal-gpu", icon: "pi pi-server", title: "GPU", tooltip: "GPUの稼働状態",
    type: "custom", render
  });
}

// Native menu group participates in ComfyUI layout; no canvas overlay or selectors.
export function registerGpuIndicator() {
  if (!app.menu?.element?.append || !app.extensionManager?.command?.execute) {
    throw new Error("GPUの常時表示にはツールバー拡張APIに対応したComfyUIが必要です。");
  }
  const indicator = new ComfyButton({
    content: "GPU(?)", tooltip: "GPUの稼働状態を確認中",
    action: () => app.extensionManager.command.execute("Workspace.ToggleSidebarTab.modal-gpu")
  });
  indicator.element.classList.add("modal-gpu-indicator");
  // A separate sibling of the existing groups avoids a shared button background.
  app.menu.element.append(indicator.element);
  return (count, detail, state) => {
    indicator.content = `GPU(${count})`;
    indicator.tooltip = `${detail} クリックで詳細を表示`;
    indicator.element.dataset.state = state;
  };
}
