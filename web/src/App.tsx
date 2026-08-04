import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  Breadcrumb,
  Breadcrumbs,
  Button,
  Checkbox,
  Dialog,
  DialogTrigger,
  FileTrigger,
  Heading,
  Input,
  Label,
  Modal,
  ModalOverlay,
  ProgressBar,
  SearchField,
  Select,
  SelectValue,
  ListBox,
  ListBoxItem,
  Tab,
  TabList,
  TabPanel,
  Tabs,
  TextField,
  Popover,
} from "react-aria-components";
import {
  contentUrl,
  createDirectory,
  deleteAssets,
  fetchAssets,
  moveAsset,
  thumbnailUrl,
  uploadFiles,
} from "./api/client";
import { formatDate, humanSize, joinVolumePath } from "./lib/format";
import type { AssetEntry, AssetListResponse, SortMode, VolumeId } from "./types";
import { SORT_OPTIONS } from "./types";

const VOLUMES: { id: VolumeId; label: string }[] = [
  { id: "comfy-inputs", label: "Inputs" },
  { id: "comfy-outputs", label: "Outputs" },
  { id: "comfy-model", label: "Models" },
];

const ASSET_PATH_ATTR = "data-asset-path";

type DragSelectState = {
  startPath: string;
  /** true = check range, false = uncheck range */
  select: boolean;
  base: Map<string, AssetEntry>;
  lastPath: string;
  moved: boolean;
};

type ViewParams = {
  volume: VolumeId;
  path: string;
  search: string;
  sort: SortMode;
  page: number;
};

/** Volume-scoped key so Inputs suppressions never hide Outputs paths. */
function suppressKey(volume: string, path: string): string {
  return `${volume}\0${path}`;
}

function isDragExemptTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  return Boolean(
    target.closest('input, a, label, [data-no-drag-select]'),
  );
}

export default function App() {
  const [volume, setVolume] = useState<VolumeId>("comfy-inputs");
  const [path, setPath] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortMode>("modified_desc");
  const [page, setPage] = useState(1);
  const [pageInput, setPageInput] = useState("1");
  const [data, setData] = useState<AssetListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadStartedAt, setLoadStartedAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  const [error, setError] = useState<string | null>(null);
  /** Multi-select for bulk delete (path -> entry). Survives page flips within a folder. */
  const [checked, setChecked] = useState<Map<string, AssetEntry>>(() => new Map());
  /** Last focused entry for preview / single move. */
  const [focused, setFocused] = useState<AssetEntry | null>(null);
  const [statusMessage, setStatusMessage] = useState("");
  const [destVolume, setDestVolume] = useState<VolumeId>("comfy-inputs");
  const [destPath, setDestPath] = useState("");
  const [moveOverwrite, setMoveOverwrite] = useState(false);
  const [uploadDest, setUploadDest] = useState("");
  const [uploadOverwrite, setUploadOverwrite] = useState(false);
  const [uploadFilesState, setUploadFilesState] = useState<File[] | null>(null);
  /** Folder name (or relative path) to create under the current directory. */
  const [newFolderName, setNewFolderName] = useState("");
  const [busy, setBusy] = useState(false);
  const [busyLabel, setBusyLabel] = useState("");
  /** Live delete progress for the toast. */
  const [deleteProgress, setDeleteProgress] = useState<{
    total: number;
    done: number;
    failed: number;
  } | null>(null);
  const requestId = useRef(0);
  /** Latest non-quiet list request — used so quiet refresh can't leave loading stuck. */
  const loadingRequestId = useRef(0);
  const lastRangeAnchor = useRef<string | null>(null);
  /**
   * Paths removed optimistically; keys are `${volume}\0${path}` so a delete on
   * Inputs never filters the same relative path on Outputs/Models.
   */
  const suppressedPaths = useRef<Set<string>>(new Set());
  const dragSelectRef = useRef<DragSelectState | null>(null);
  /** After a drag-select, ignore the synthetic click that follows pointerup. */
  const suppressClickRef = useRef(false);
  const pageEntriesRef = useRef<AssetEntry[]>([]);
  const checkedRef = useRef<Map<string, AssetEntry>>(new Map());
  /** Always-current view; load/delete must not use a stale volume from an older render. */
  const viewRef = useRef<ViewParams>({
    volume,
    path,
    search,
    sort,
    page,
  });
  viewRef.current = { volume, path, search, sort, page };

  const applyListResult = useCallback((result: AssetListResponse) => {
    const hidden = suppressedPaths.current;
    if (!hidden.size) {
      setData(result);
      return;
    }
    const entries = result.entries.filter(
      (entry) => !hidden.has(suppressKey(result.volume, entry.path)),
    );
    const removedHere = result.entries.length - entries.length;
    // Drop suppressions for this volume that Modal no longer returns (fully gone).
    for (const key of [...hidden]) {
      const sep = key.indexOf("\0");
      if (sep < 0) {
        hidden.delete(key);
        continue;
      }
      const keyVolume = key.slice(0, sep);
      const keyPath = key.slice(sep + 1);
      if (keyVolume !== result.volume) continue;
      const stillListed = result.entries.some((entry) => entry.path === keyPath);
      if (!stillListed) hidden.delete(key);
    }
    setData({
      ...result,
      entries,
      total: Math.max(0, result.total - removedHere),
      status: result.status + (removedHere ? " · 削除反映済み" : ""),
    });
  }, []);

  const load = useCallback(
    async (options: { refresh?: boolean; quiet?: boolean } = {}) => {
      // Snapshot at call time so post-delete refresh uses the tab the user is on now,
      // not the Inputs/Outputs/Models tab from when the delete started.
      const view = { ...viewRef.current };
      const id = ++requestId.current;
      if (!options.quiet) {
        loadingRequestId.current = id;
        setLoading(true);
        setLoadStartedAt(Date.now());
      }
      setError(null);
      try {
        const result = await fetchAssets({
          volume: view.volume,
          path: view.path,
          search: view.search,
          sort: view.sort,
          page: view.page,
          pageSize: 100,
          refresh: options.refresh,
        });
        if (id !== requestId.current) return;
        const current = viewRef.current;
        // User switched volume/folder/page while this request was in flight.
        if (
          current.volume !== view.volume ||
          current.path !== view.path ||
          current.search !== view.search ||
          current.sort !== view.sort ||
          current.page !== view.page
        ) {
          return;
        }
        applyListResult(result);
        if (result.page !== view.page) {
          setPage(result.page);
        }
      } catch (err) {
        if (id !== requestId.current) return;
        const current = viewRef.current;
        if (
          current.volume !== view.volume ||
          current.path !== view.path ||
          current.search !== view.search ||
          current.sort !== view.sort ||
          current.page !== view.page
        ) {
          return;
        }
        setError(err instanceof Error ? err.message : String(err));
        if (!options.refresh && !options.quiet) setData(null);
      } finally {
        // Quiet (background) loads must not leave a superseded non-quiet load stuck.
        if (!options.quiet && loadingRequestId.current === id) {
          setLoading(false);
        }
      }
    },
    [applyListResult],
  );

  // Reload when the visible view changes (volume tab, folder, search, sort, page).
  useEffect(() => {
    void load();
  }, [volume, path, search, sort, page, load]);

  useEffect(() => {
    if (!loading && !busy) return;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [loading, busy]);

  const elapsedSec = Math.max(0, Math.round((now - loadStartedAt) / 1000));
  const showBootOverlay = loading && !data && !error;

  useEffect(() => {
    setUploadDest(path);
  }, [path]);

  // Inputs/Outputs: folders + images + videos in the card gallery (folders first),
  // remaining files in the table. Models: all in table.
  const displayTable = useMemo(() => {
    if (volume === "comfy-model") return data?.entries ?? [];
    return (data?.entries ?? []).filter(
      (entry) =>
        !entry.is_directory &&
        entry.media_type !== "image" &&
        entry.media_type !== "video",
    );
  }, [data, volume]);

  const displayGallery = useMemo(() => {
    if (volume === "comfy-model") return [];
    const items = (data?.entries ?? []).filter(
      (entry) =>
        entry.is_directory ||
        entry.media_type === "image" ||
        entry.media_type === "video",
    );
    // Folders first so nested structure is obvious among media cards.
    return [...items].sort(
      (a, b) => Number(b.is_directory) - Number(a.is_directory),
    );
  }, [data, volume]);

  // Warm thumb cache for the current page — low concurrency so a user click's
  // sidebar preview (full content) can jump the queue on the server and in the
  // browser connection pool.
  const focusedPath = focused?.path ?? null;
  useEffect(() => {
    const media = displayGallery.filter(
      (entry) =>
        (entry.media_type === "image" || entry.media_type === "video") &&
        entry.path !== focusedPath,
    );
    if (!media.length) return;

    let cancelled = false;
    const controllers: AbortController[] = [];
    const concurrency = 2;

    async function run() {
      // Let the click's content request leave the browser first.
      await new Promise((resolve) => window.setTimeout(resolve, 80));
      if (cancelled) return;

      let next = 0;
      async function worker() {
        while (!cancelled) {
          const index = next;
          next += 1;
          const entry = media[index];
          if (!entry) return;
          const url = thumbnailUrl(entry.volume, entry.path, {
            name: entry.name,
            size: entry.size,
            modified_at: entry.modified_at,
            media_type: entry.media_type,
          });
          const controller = new AbortController();
          controllers.push(controller);
          try {
            await fetch(url, {
              signal: controller.signal,
              cache: "force-cache",
              // Chromium: deprioritize vs sidebar preview fetch.
              priority: "low",
              headers: { Accept: "image/*" },
            } as RequestInit);
          } catch {
            /* best-effort prefetch */
          }
        }
      }

      await Promise.all(
        Array.from({ length: concurrency }, () => worker()),
      );
    }

    void run();
    return () => {
      cancelled = true;
      for (const controller of controllers) controller.abort();
    };
  }, [displayGallery, focusedPath]);

  // Kick full-content load for the sidebar as soon as selection changes so it
  // doesn't wait on <img>/<video> element scheduling behind gallery thumbs.
  useEffect(() => {
    if (!focused || focused.is_directory) return;
    if (
      focused.media_type !== "image" &&
      focused.media_type !== "video" &&
      focused.media_type !== "audio"
    ) {
      return;
    }
    const url = contentUrl(focused.volume, focused.path, false, focused);
    const controller = new AbortController();
    void fetch(url, {
      signal: controller.signal,
      // Chromium: prefer this over thumbnail traffic.
      priority: "high",
      cache: "force-cache",
    } as RequestInit).catch(() => {
      /* element src will retry */
    });
    return () => controller.abort();
  }, [focused]);

  const pageEntries = useMemo(
    () => [...displayGallery, ...displayTable],
    [displayGallery, displayTable],
  );
  pageEntriesRef.current = pageEntries;
  checkedRef.current = checked;

  const checkedList = useMemo(() => [...checked.values()], [checked]);
  const checkedCount = checkedList.length;
  const pageAllChecked =
    pageEntries.length > 0 && pageEntries.every((entry) => checked.has(entry.path));
  const pageSomeChecked = pageEntries.some((entry) => checked.has(entry.path));

  const wasMultiMoveRef = useRef(false);
  // Single-select: default dest to the focused/checked item path (rename/move).
  // Multi-select: default dest to the current folder once when bulk mode starts.
  useEffect(() => {
    const multi = checkedCount > 1;
    if (multi) {
      if (!wasMultiMoveRef.current) {
        setDestVolume(volume);
        setDestPath(path);
      }
      wasMultiMoveRef.current = true;
      return;
    }
    wasMultiMoveRef.current = false;
    if (focused) {
      setDestVolume(focused.volume as VolumeId);
      setDestPath(focused.path);
      return;
    }
    if (checkedCount === 1) {
      const only = checkedList[0];
      if (only) {
        setDestVolume(only.volume as VolumeId);
        setDestPath(only.path);
      }
      return;
    }
    setDestPath("");
  }, [focused, checkedCount, checkedList, path, volume]);

  useEffect(() => {
    setPageInput(String(page));
  }, [page]);

  useEffect(() => {
    function entryFromPoint(clientX: number, clientY: number): AssetEntry | null {
      const el = document.elementFromPoint(clientX, clientY);
      if (!(el instanceof Element)) return null;
      const host = el.closest(`[${ASSET_PATH_ATTR}]`);
      if (!(host instanceof HTMLElement)) return null;
      const path = host.getAttribute(ASSET_PATH_ATTR);
      if (!path) return null;
      return pageEntriesRef.current.find((entry) => entry.path === path) ?? null;
    }

    function applyDragTo(entry: AssetEntry) {
      const drag = dragSelectRef.current;
      if (!drag) return;
      const entries = pageEntriesRef.current;
      const from = entries.findIndex((item) => item.path === drag.startPath);
      const to = entries.findIndex((item) => item.path === entry.path);
      if (from < 0 || to < 0) return;
      // Require leaving the start item so a plain click still goes through onClick.
      if (entry.path !== drag.startPath) drag.moved = true;
      if (!drag.moved) return;
      if (entry.path === drag.lastPath) return;
      drag.lastPath = entry.path;
      const [start, end] = from < to ? [from, to] : [to, from];
      setChecked(() => {
        const map = new Map(drag.base);
        for (let i = start; i <= end; i += 1) {
          const item = entries[i];
          if (!item) continue;
          if (drag.select) map.set(item.path, item);
          else map.delete(item.path);
        }
        return map;
      });
      lastRangeAnchor.current = entry.path;
      setFocused(entry);
    }

    function onPointerMove(event: PointerEvent) {
      if (!dragSelectRef.current) return;
      if (event.buttons === 0) {
        endDragSelect();
        return;
      }
      const entry = entryFromPoint(event.clientX, event.clientY);
      if (entry) applyDragTo(entry);
    }

    function endDragSelect() {
      const drag = dragSelectRef.current;
      if (!drag) return;
      if (drag.moved) suppressClickRef.current = true;
      dragSelectRef.current = null;
      document.body.classList.remove("is-drag-selecting");
    }

    function onPointerUp() {
      endDragSelect();
    }

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    window.addEventListener("pointercancel", onPointerUp);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      window.removeEventListener("pointercancel", onPointerUp);
    };
  }, []);

  function goToPageInput() {
    if (!data) {
      setPageInput(String(page));
      return;
    }
    const parsed = Number.parseInt(pageInput.trim(), 10);
    if (!Number.isFinite(parsed)) {
      setPageInput(String(page));
      return;
    }
    const clamped = Math.min(data.page_count, Math.max(1, parsed));
    setPageInput(String(clamped));
    if (clamped !== page) setPage(clamped);
  }

  function clearChecks() {
    setChecked(new Map());
    lastRangeAnchor.current = null;
  }

  function focusEntry(entry: AssetEntry) {
    setFocused(entry);
  }

  function consumeSuppressedClick(): boolean {
    if (!suppressClickRef.current) return false;
    suppressClickRef.current = false;
    return true;
  }

  function beginDragSelect(entry: AssetEntry, event: ReactPointerEvent) {
    if (event.button !== 0) return;
    if (event.shiftKey || event.metaKey || event.ctrlKey) return;
    if (isDragExemptTarget(event.target)) return;

    // Always check the painted range. Uncheck via checkbox / ⌘·Ctrl+click.
    dragSelectRef.current = {
      startPath: entry.path,
      select: true,
      base: new Map(checkedRef.current),
      lastPath: entry.path,
      moved: false,
    };
    document.body.classList.add("is-drag-selecting");
  }

  function toggleCheck(entry: AssetEntry, next?: boolean) {
    setChecked((prev) => {
      const map = new Map(prev);
      const shouldSelect = next ?? !map.has(entry.path);
      if (shouldSelect) map.set(entry.path, entry);
      else map.delete(entry.path);
      return map;
    });
    lastRangeAnchor.current = entry.path;
    focusEntry(entry);
  }

  function selectRange(toEntry: AssetEntry) {
    const anchor = lastRangeAnchor.current;
    if (!anchor) {
      toggleCheck(toEntry, true);
      return;
    }
    const paths = pageEntries.map((entry) => entry.path);
    const from = paths.indexOf(anchor);
    const to = paths.indexOf(toEntry.path);
    if (from < 0 || to < 0) {
      toggleCheck(toEntry, true);
      return;
    }
    const [start, end] = from < to ? [from, to] : [to, from];
    setChecked((prev) => {
      const map = new Map(prev);
      for (let i = start; i <= end; i += 1) {
        const entry = pageEntries[i];
        if (entry) map.set(entry.path, entry);
      }
      return map;
    });
    focusEntry(toEntry);
  }

  function setPageChecked(select: boolean) {
    setChecked((prev) => {
      const map = new Map(prev);
      for (const entry of pageEntries) {
        if (select) map.set(entry.path, entry);
        else map.delete(entry.path);
      }
      return map;
    });
  }

  function switchVolume(next: VolumeId) {
    setVolume(next);
    setPath("");
    setPage(1);
    setSearch("");
    setSearchInput("");
    setFocused(null);
    clearChecks();
    setDestVolume(next);
    setUploadDest("");
    setStatusMessage("");
    setError(null);
  }

  async function navigateTo(nextPath: string) {
    setPath(nextPath);
    setPage(1);
    setFocused(null);
    clearChecks();
  }

  async function openFolder(entry: AssetEntry) {
    if (!entry.is_directory) return;
    await navigateTo(entry.path);
  }

  function removePathsFromView(volumeId: string, paths: string[]) {
    if (!paths.length) return;
    for (const pathValue of paths) {
      suppressedPaths.current.add(suppressKey(volumeId, pathValue));
    }
    const removed = new Set(paths);
    setData((prev) => {
      // Only touch the list if we're still looking at the volume we mutated.
      if (!prev || prev.volume !== volumeId) return prev;
      const entries = prev.entries.filter((entry) => !removed.has(entry.path));
      const removedCount = prev.entries.length - entries.length;
      if (!removedCount) return prev;
      const total = Math.max(0, prev.total - removedCount);
      const imageRemoved = prev.entries.filter(
        (entry) => removed.has(entry.path) && entry.media_type === "image",
      ).length;
      const imageTotal = Math.max(0, prev.image_total - imageRemoved);
      const label = VOLUMES.find((item) => item.id === volumeId)?.label ?? volumeId;
      return {
        ...prev,
        entries,
        total,
        image_total: imageTotal,
        status: `${label}: ${total}件 / 画像 ${imageTotal}件（削除を画面に反映）`,
      };
    });
  }

  function clearSuppressedFor(volumeId: string, paths: string[]) {
    for (const pathValue of paths) {
      suppressedPaths.current.delete(suppressKey(volumeId, pathValue));
    }
  }

  /** Bust list cache for a volume the user is no longer viewing (e.g. deleted while on another tab). */
  function bustVolumeListCache(volumeId: string) {
    void fetchAssets({
      volume: volumeId,
      path: "",
      page: 1,
      pageSize: 1,
      refresh: true,
    }).catch(() => {
      /* best-effort cache invalidation */
    });
  }

  async function syncListAfterMutation(sourceVolume: string) {
    const current = viewRef.current;
    if (current.volume === sourceVolume) {
      await load({ refresh: true, quiet: true });
      return;
    }
    // User switched Inputs/Outputs/Models mid-flight: don't overwrite the new tab
    // with the source volume's list; just invalidate the source cache.
    bustVolumeListCache(sourceVolume);
  }

  async function handleCreateFolder() {
    const name = newFolderName.trim().replace(/^\/+|\/+$/g, "");
    if (!name) {
      setStatusMessage("フォルダ名を入力してください。");
      return;
    }
    if (name.includes("\\") || name.includes("\0") || name.split("/").some((part) => part === "..")) {
      setStatusMessage("フォルダ名に .. や不正な文字は使えません。");
      return;
    }
    const mkdirVolume = volume;
    const fullPath = joinVolumePath(path, name);
    setBusy(true);
    setDeleteProgress(null);
    setBusyLabel("フォルダ作成中…");
    setStatusMessage(`フォルダ作成中: ${fullPath}`);
    try {
      const result = await createDirectory({
        volume: mkdirVolume,
        path: fullPath,
      });
      setStatusMessage(result.message);
      setNewFolderName("");
      setBusyLabel("一覧を同期中…");
      await syncListAfterMutation(mkdirVolume);
      setStatusMessage(result.message);
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setBusyLabel("");
    }
  }

  async function handleUpload() {
    if (!uploadFilesState?.length) {
      setStatusMessage("アップロードするファイルを選択してください。");
      return;
    }
    const uploadVolume = volume;
    setBusy(true);
    setDeleteProgress(null);
    setBusyLabel(`アップロード中（${uploadFilesState.length}件）…`);
    setStatusMessage(`アップロード中（${uploadFilesState.length}件）…`);
    try {
      const result = await uploadFiles({
        volume: uploadVolume,
        destination: uploadDest,
        overwrite: uploadOverwrite,
        files: uploadFilesState,
      });
      setStatusMessage(result.message);
      setUploadFilesState(null);
      setBusyLabel("一覧を同期中…");
      await syncListAfterMutation(uploadVolume);
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setBusyLabel("");
    }
  }

  async function handleMove() {
    const targets = checkedCount > 0 ? checkedList : focused ? [focused] : [];
    if (!targets.length) {
      setStatusMessage("移動する項目を選択してください。");
      return;
    }
    const volumes = new Set(targets.map((entry) => entry.volume));
    if (volumes.size !== 1) {
      setStatusMessage("複数 Volume をまたいだ一括移動はできません。同じ Volume 内で選択してください。");
      return;
    }
    const sourceVolume = targets[0]!.volume;
    const multi = targets.length > 1;
    const destFolder = destPath.trim();
    if (!multi && !destFolder) {
      setStatusMessage("移動先パスを入力してください。");
      return;
    }

    const jobs = targets.map((entry) => ({
      entry,
      destinationPath: multi
        ? joinVolumePath(destFolder, entry.name)
        : destFolder,
    }));

    // Same-path no-ops waste API calls; drop them early.
    const actionable = jobs.filter(
      (job) =>
        !(
          sourceVolume === destVolume &&
          job.entry.path === job.destinationPath
        ),
    );
    if (!actionable.length) {
      setStatusMessage("移動元と移動先が同じです。");
      return;
    }

    const total = actionable.length;
    setBusy(true);
    setDeleteProgress({ total, done: 0, failed: 0 });
    setBusyLabel(multi ? `移動中 0/${total}` : `移動中: ${actionable[0]!.entry.path}`);
    setStatusMessage(
      multi
        ? `${total}件を ${destVolume}:${destFolder || "/"} へ移動中…`
        : `移動中: ${actionable[0]!.entry.path}`,
    );

    const movedPaths = actionable.map((job) => job.entry.path);
    // Optimistic: drop from current folder view immediately.
    removePathsFromView(sourceVolume, movedPaths);
    setFocused(null);
    clearChecks();

    let done = 0;
    let failed = 0;
    const failures: { path: string; error: string }[] = [];
    try {
      for (const job of actionable) {
        try {
          await moveAsset({
            source_volume: sourceVolume,
            source_path: job.entry.path,
            destination_volume: destVolume,
            destination_path: job.destinationPath,
            overwrite: moveOverwrite,
          });
          done += 1;
        } catch (err) {
          failed += 1;
          const error = err instanceof Error ? err.message : String(err);
          failures.push({ path: job.entry.path, error });
          // Put failed items back into the list.
          clearSuppressedFor(sourceVolume, [job.entry.path]);
        }
        const processed = done + failed;
        setDeleteProgress({ total, done, failed });
        setBusyLabel(
          `移動中 ${processed}/${total}（成功 ${done}${failed ? ` / 失敗 ${failed}` : ""}）`,
        );
        setStatusMessage(
          `移動進捗: 成功 ${done} / 失敗 ${failed} / 合計 ${total}`,
        );
      }

      let message =
        failed === 0
          ? multi
            ? `${done}件を移動しました → ${destVolume}:${destFolder || "/"}`
            : `移動完了: ${destVolume}:${actionable[0]!.destinationPath}`
          : `移動: 成功 ${done}件 / 失敗 ${failed}件`;
      if (failures.length) {
        message +=
          " — " +
          failures
            .slice(0, 3)
            .map((item) => `${item.path}: ${item.error}`)
            .join("; ");
      }
      setDeleteProgress({ total, done, failed });
      setStatusMessage(message);
      setBusyLabel(
        failed === 0
          ? `移動完了 ${done}件 · 一覧同期中…`
          : `成功 ${done} / 失敗 ${failed} · 一覧同期中…`,
      );
      await syncListAfterMutation(sourceVolume);
      if (destVolume !== sourceVolume) bustVolumeListCache(destVolume);
      setStatusMessage(message);
      setBusyLabel(
        failed === 0 ? `移動完了: ${done}件` : `成功 ${done}件 / 失敗 ${failed}件`,
      );
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : String(err));
      clearSuppressedFor(sourceVolume, movedPaths);
      await syncListAfterMutation(sourceVolume);
    } finally {
      setBusy(false);
      setBusyLabel("");
    }
  }

  async function handleDeleteSelected() {
    const targets = checkedCount > 0 ? checkedList : focused ? [focused] : [];
    if (!targets.length) {
      setStatusMessage("削除する項目を選択してください。");
      return;
    }
    // Capture volume at start — user may switch Inputs/Outputs/Models during delete.
    const deleteVolume = volume;
    const targetPaths = targets.map((entry) => entry.path);
    const total = targets.length;
    setBusy(true);
    setDeleteProgress({ total, done: 0, failed: 0 });
    setBusyLabel(`削除中 0/${total}（成功 0）`);
    setStatusMessage(`${total}件を削除中…`);
    // Optimistic: drop from view up front (no-op for list if user already left this tab).
    removePathsFromView(deleteVolume, targetPaths);
    setFocused(null);
    clearChecks();

    try {
      const result = await deleteAssets({
        volume: deleteVolume,
        items: targets.map((entry) => ({
          path: entry.path,
          recursive: entry.is_directory,
        })),
        workers: 4,
        onProgress: (progress) => {
          setDeleteProgress({
            total: progress.total,
            done: progress.done,
            failed: progress.failed,
          });
          setBusyLabel(
            `削除中 ${progress.processed}/${progress.total}（成功 ${progress.done}${
              progress.failed ? ` / 失敗 ${progress.failed}` : ""
            }）`,
          );
          setStatusMessage(
            `削除進捗: 成功 ${progress.done} / 失敗 ${progress.failed} / 合計 ${progress.total}`,
          );
        },
      });

      const done = result.paths?.length ?? result.done ?? 0;
      const failed = result.failed?.length ?? result.failed_count ?? 0;
      for (const pathValue of result.paths ?? []) {
        suppressedPaths.current.add(suppressKey(deleteVolume, pathValue));
      }
      for (const item of result.failed ?? []) {
        suppressedPaths.current.delete(suppressKey(deleteVolume, item.path));
      }

      let message = result.message;
      if (result.failed?.length) {
        message +=
          " — " +
          result.failed
            .slice(0, 3)
            .map((item) => `${item.path}: ${item.error}`)
            .join("; ");
      }
      setDeleteProgress({ total, done, failed });
      setStatusMessage(message);
      setBusyLabel(
        failed === 0
          ? `削除完了 ${done}件 · 一覧同期中…`
          : `成功 ${done} / 失敗 ${failed} · 一覧同期中…`,
      );
      await syncListAfterMutation(deleteVolume);
      setStatusMessage(message);
      setBusyLabel(
        failed === 0 ? `削除完了: ${done}件` : `成功 ${done}件 / 失敗 ${failed}件`,
      );
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : String(err));
      clearSuppressedFor(deleteVolume, targetPaths);
      await syncListAfterMutation(deleteVolume);
    } finally {
      setBusy(false);
      setBusyLabel("");
    }
  }

  const deleteTargets = checkedCount > 0 ? checkedList : focused ? [focused] : [];
  const deleteDirCount = deleteTargets.filter((entry) => entry.is_directory).length;

  return (
    <div className="relative mx-auto flex min-h-screen max-w-7xl flex-col gap-4 px-4 py-6">
      {showBootOverlay && (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-zinc-950/90 px-6"
          role="status"
          aria-live="polite"
        >
          <div className="w-full max-w-md rounded-xl border border-zinc-800 bg-zinc-900 p-6 shadow-2xl">
            <p className="text-base font-medium text-zinc-100">
              {VOLUMES.find((item) => item.id === volume)?.label ?? volume} を読み込み中
            </p>
            <p className="mt-2 text-sm leading-relaxed text-zinc-400">
              Modal Volume の一覧を取得しています。Inputs は件数が多いと初回だけ十数秒かかることがあります。
              ページ送りや 2 回目以降はキャッシュで速くなります。
            </p>
            <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-zinc-800">
              <div className="h-full w-1/3 animate-pulse rounded-full bg-sky-500" />
            </div>
            <p className="mt-3 text-xs text-zinc-500">{elapsedSec} 秒経過</p>
          </div>
        </div>
      )}

      {(busy || statusMessage || deleteProgress) && (
        <div
          className={`fixed top-4 right-4 z-50 w-[min(22rem,calc(100vw-2rem))] rounded-lg border px-4 py-3 shadow-2xl ${
            busy
              ? "border-amber-700/60 bg-amber-950 text-amber-50"
              : "border-emerald-800/60 bg-emerald-950 text-emerald-50"
          }`}
          role="status"
          aria-live="polite"
        >
          <p className="text-sm font-medium">
            {busy ? busyLabel || "処理中…" : "完了"}
          </p>
          {deleteProgress && (
            <p className="mt-1 text-sm font-semibold tabular-nums tracking-tight">
              成功 {deleteProgress.done} 件
              {deleteProgress.failed > 0 ? ` / 失敗 ${deleteProgress.failed} 件` : ""}
              <span className="font-normal opacity-80">
                {" "}
                （{deleteProgress.done + deleteProgress.failed}/{deleteProgress.total}）
              </span>
            </p>
          )}
          <p className="mt-1 text-xs opacity-90 break-all">
            {statusMessage || busyLabel}
          </p>
          {busy && deleteProgress && deleteProgress.total > 0 && (
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-black/30">
              <div
                className="h-full rounded-full bg-amber-400 transition-[width] duration-150"
                style={{
                  width: `${Math.min(
                    100,
                    ((deleteProgress.done + deleteProgress.failed) /
                      deleteProgress.total) *
                      100,
                  )}%`,
                }}
              />
            </div>
          )}
          {busy && !deleteProgress && (
            <div className="mt-2 h-1 overflow-hidden rounded-full bg-black/30">
              <div className="h-full w-1/2 animate-pulse rounded-full bg-amber-400" />
            </div>
          )}
        </div>
      )}

      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          Modal ComfyUI Asset Manager
        </h1>
        <p className="text-sm text-zinc-400">
          大量ファイルの整理用。チェックで複数選択 → 一括削除できます（完全削除・取り消し不可）。
          削除すると一覧から即消え、右上に成功件数の進捗が出ます。
        </p>
      </header>

      <Tabs
        selectedKey={volume}
        onSelectionChange={(key) => switchVolume(String(key) as VolumeId)}
        className="flex flex-col gap-4"
      >
        <TabList className="flex gap-2 border-b border-zinc-800 pb-2">
          {VOLUMES.map((item) => (
            <Tab
              key={item.id}
              id={item.id}
              className="cursor-pointer rounded-md px-3 py-1.5 text-sm text-zinc-300 outline-none data-[selected]:bg-sky-500/20 data-[selected]:text-sky-200 data-[hovered]:bg-zinc-800"
            >
              {item.label}
            </Tab>
          ))}
        </TabList>

        {VOLUMES.map((item) => (
          <TabPanel key={item.id} id={item.id} className="outline-none">
            <div className="grid gap-4 lg:grid-cols-[minmax(0,1.6fr)_minmax(320px,1fr)]">
              <section className="flex min-w-0 flex-col gap-3">
                <div className="flex flex-wrap items-end gap-3">
                  <div className="min-w-[12rem] flex-1">
                    <Label className="mb-1 block text-xs text-zinc-400">現在のフォルダ</Label>
                    <Breadcrumbs className="flex flex-wrap items-center gap-1 text-sm">
                      {(data?.breadcrumb ?? [{ label: "/", path: "" }]).map((crumb) => (
                        <Breadcrumb key={crumb.path || "root"} className="flex items-center gap-1">
                          <Button
                            className="rounded px-1 text-sky-300 hover:underline"
                            onPress={() => void navigateTo(crumb.path)}
                          >
                            {crumb.label}
                          </Button>
                          <span className="text-zinc-600">/</span>
                        </Breadcrumb>
                      ))}
                    </Breadcrumbs>
                  </div>
                  <Button
                    className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm hover:bg-zinc-800"
                    onPress={() => {
                      const parent =
                        path.includes("/")
                          ? path.split("/").slice(0, -1).join("/")
                          : "";
                      void navigateTo(parent);
                    }}
                    isDisabled={!path}
                  >
                    一つ上へ
                  </Button>
                  <Button
                    className="rounded-md border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm hover:bg-zinc-800"
                    onPress={() => {
                      setStatusMessage("一覧を強制更新中…");
                      void load({ refresh: true }).then(() =>
                        setStatusMessage("一覧を更新しました"),
                      );
                    }}
                  >
                    更新
                  </Button>
                </div>

                <div className="flex flex-wrap items-end gap-3">
                  <SearchField
                    value={searchInput}
                    onChange={setSearchInput}
                    onSubmit={() => {
                      setPage(1);
                      setSearch(searchInput.trim());
                    }}
                    className="min-w-[16rem] flex-1"
                  >
                    <Label className="mb-1 block text-xs text-zinc-400">
                      検索（現在のフォルダ内・名前の部分一致）
                    </Label>
                    <div className="flex gap-2">
                      <Input
                        className="min-w-0 flex-1 rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm"
                        placeholder="例: camera-stream / .png"
                      />
                      <Button
                        className="shrink-0 rounded-md bg-sky-600 px-3 py-2 text-sm text-white hover:bg-sky-500"
                        onPress={() => {
                          setPage(1);
                          setSearch(searchInput.trim());
                        }}
                      >
                        検索
                      </Button>
                      <Button
                        className="shrink-0 rounded-md border border-zinc-700 px-3 py-2 text-sm hover:bg-zinc-800"
                        onPress={() => {
                          setSearchInput("");
                          setPage(1);
                          setSearch("");
                        }}
                        isDisabled={!search && !searchInput}
                      >
                        クリア
                      </Button>
                    </div>
                  </SearchField>

                  <Select
                    selectedKey={sort}
                    onSelectionChange={(key) => {
                      setSort(String(key) as SortMode);
                      setPage(1);
                    }}
                    className="min-w-[12rem]"
                  >
                    <Label className="mb-1 block text-xs text-zinc-400">並べ替え</Label>
                    <Button className="flex w-full items-center justify-between rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-left text-sm">
                      <SelectValue />
                      <span aria-hidden>▾</span>
                    </Button>
                    <Popover className="rounded-md border border-zinc-700 bg-zinc-900 p-1 shadow-xl">
                      <ListBox className="outline-none">
                        {SORT_OPTIONS.map((option) => (
                          <ListBoxItem
                            key={option.id}
                            id={option.id}
                            className="cursor-pointer rounded px-3 py-1.5 text-sm outline-none data-[focused]:bg-zinc-800"
                          >
                            {option.label}
                          </ListBoxItem>
                        ))}
                      </ListBox>
                    </Popover>
                  </Select>
                </div>

                <div className="flex flex-wrap items-center gap-3 text-sm text-zinc-400">
                  <span>{data?.status ?? (loading ? "読み込み中…" : "—")}</span>
                  {(loading || busy) && (
                    <ProgressBar isIndeterminate aria-label="処理中" className="text-sky-300">
                      {() => (
                        <span className="text-xs text-sky-300/90">
                          {busy
                            ? busyLabel || "処理中…"
                            : `Modal に問い合わせ中… ${elapsedSec}s`}
                        </span>
                      )}
                    </ProgressBar>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-2 rounded-md border border-zinc-800 bg-zinc-900/50 px-3 py-2">
                  <Checkbox
                    isSelected={pageAllChecked}
                    isIndeterminate={pageSomeChecked && !pageAllChecked}
                    onChange={(value) => setPageChecked(value)}
                    isDisabled={!pageEntries.length}
                    className="flex items-center gap-2 text-sm text-zinc-200"
                  >
                    <div className="flex h-4 w-4 items-center justify-center rounded border border-zinc-600 data-[selected]:bg-sky-600 data-[indeterminate]:bg-sky-700">
                      {pageAllChecked ? "✓" : pageSomeChecked ? "–" : ""}
                    </div>
                    このページを全選択
                  </Checkbox>
                  <span className="text-xs text-zinc-500">
                    選択中 {checkedCount} 件
                    {checkedCount > 0 && pageEntries.length
                      ? `（うちこのページ ${pageEntries.filter((e) => checked.has(e.path)).length}）`
                      : ""}
                  </span>
                  <Button
                    className="rounded border border-zinc-700 px-2 py-1 text-xs hover:bg-zinc-800"
                    onPress={clearChecks}
                    isDisabled={!checkedCount}
                  >
                    選択解除
                  </Button>
                  <DialogTrigger>
                    <Button
                      className="rounded bg-red-700 px-2.5 py-1 text-xs text-white hover:bg-red-600 disabled:opacity-40"
                      isDisabled={!deleteTargets.length || busy}
                    >
                      選択を削除（{deleteTargets.length}）
                    </Button>
                    <ModalOverlay className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
                      <Modal className="w-full max-w-lg rounded-lg border border-zinc-700 bg-zinc-950 p-4 shadow-2xl">
                        <Dialog className="outline-none">
                          {({ close }) => (
                            <div className="space-y-3">
                              <Heading
                                slot="title"
                                className="text-lg font-semibold text-red-200"
                              >
                                一括完全削除の確認
                              </Heading>
                              <p className="text-sm text-zinc-300">
                                {volume} から{" "}
                                <strong className="text-zinc-100">{deleteTargets.length} 件</strong>
                                を完全に削除します。
                                {deleteDirCount > 0
                                  ? ` うちフォルダ ${deleteDirCount} 件は配下ごと再帰削除します。`
                                  : ""}
                                取り消せません。
                              </p>
                              <ul className="max-h-40 overflow-auto rounded border border-zinc-800 bg-zinc-900/60 p-2 text-xs text-zinc-400">
                                {deleteTargets.slice(0, 50).map((entry) => (
                                  <li key={entry.path} className="truncate">
                                    {entry.is_directory ? "📁 " : ""}
                                    {entry.path}
                                  </li>
                                ))}
                                {deleteTargets.length > 50 && (
                                  <li>…ほか {deleteTargets.length - 50} 件</li>
                                )}
                              </ul>
                              <div className="flex justify-end gap-2">
                                <Button
                                  className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm"
                                  onPress={close}
                                >
                                  キャンセル
                                </Button>
                                <Button
                                  className="rounded-md bg-red-700 px-3 py-1.5 text-sm text-white"
                                  onPress={() => {
                                    close();
                                    void handleDeleteSelected();
                                  }}
                                >
                                  完全に削除する
                                </Button>
                              </div>
                            </div>
                          )}
                        </Dialog>
                      </Modal>
                    </ModalOverlay>
                  </DialogTrigger>
                  <span className="text-[11px] text-zinc-600">
                    ドラッグまたは Shift+クリックで範囲選択
                  </span>
                </div>

                {error && (
                  <div className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-sm text-red-200">
                    <p>{error}</p>
                    <Button
                      className="mt-2 rounded border border-red-800 px-2 py-1 text-xs"
                      onPress={() => void load({ refresh: true })}
                    >
                      再試行
                    </Button>
                  </div>
                )}

                {displayGallery.length > 0 && (
                  <div>
                    <h2 className="mb-2 text-sm font-medium text-zinc-300">
                      フォルダ・画像・動画
                    </h2>
                    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
                      {displayGallery.map((entry) => {
                        const isChecked = checked.has(entry.path);
                        const isFocused = focused?.path === entry.path;
                        const isVideo = entry.media_type === "video";
                        const isDir = entry.is_directory;
                        return (
                          <div
                            key={entry.path}
                            {...{ [ASSET_PATH_ATTR]: entry.path }}
                            className={`relative overflow-hidden rounded-md border bg-zinc-900 text-left ${
                              isChecked
                                ? "border-sky-500 ring-1 ring-sky-500/40"
                                : isFocused
                                  ? "border-sky-700"
                                  : isDir
                                    ? "border-amber-800/70 hover:border-amber-600/80"
                                    : "border-zinc-800 hover:border-zinc-600"
                            }`}
                            onPointerDown={(event) => beginDragSelect(entry, event)}
                          >
                            <label className="absolute left-1.5 top-1.5 z-10 flex h-6 w-6 cursor-pointer items-center justify-center rounded bg-black/60">
                              <input
                                type="checkbox"
                                className="h-3.5 w-3.5 accent-sky-500"
                                checked={isChecked}
                                onChange={(event) =>
                                  toggleCheck(entry, event.target.checked)
                                }
                                onClick={(event) => event.stopPropagation()}
                              />
                            </label>
                            <span
                              className={`pointer-events-none absolute right-1.5 top-1.5 z-10 rounded px-1.5 py-0.5 text-[10px] font-medium tracking-wide ${
                                isDir
                                  ? "bg-amber-900/90 text-amber-100"
                                  : isVideo
                                    ? "bg-black/70 text-zinc-100"
                                    : "hidden"
                              }`}
                            >
                              {isDir ? "FOLDER" : isVideo ? "VIDEO" : ""}
                            </span>
                            <button
                              type="button"
                              className="block w-full text-left"
                              onClick={(event) => {
                                if (consumeSuppressedClick()) return;
                                if (event.shiftKey) selectRange(entry);
                                else if (event.metaKey || event.ctrlKey) {
                                  toggleCheck(entry);
                                } else {
                                  focusEntry(entry);
                                  if (!checked.has(entry.path)) toggleCheck(entry, true);
                                }
                              }}
                              onDoubleClick={() => {
                                if (isDir) void openFolder(entry);
                              }}
                            >
                              {isDir ? (
                                <div className="flex aspect-square w-full flex-col items-center justify-center gap-2 bg-gradient-to-b from-amber-950/50 to-zinc-950 pointer-events-none">
                                  <span className="text-5xl leading-none" aria-hidden>
                                    📁
                                  </span>
                                  <span className="text-[11px] text-amber-200/80">
                                    ダブルクリックで開く
                                  </span>
                                </div>
                              ) : (
                                <img
                                  src={thumbnailUrl(entry.volume, entry.path, {
                                    name: entry.name,
                                    size: entry.size,
                                    modified_at: entry.modified_at,
                                    media_type: entry.media_type,
                                  })}
                                  alt={entry.name}
                                  draggable={false}
                                  loading="lazy"
                                  decoding="async"
                                  // Keep gallery thumbs below sidebar preview in the network scheduler.
                                  fetchPriority="low"
                                  className="aspect-square w-full object-contain bg-black/40 pointer-events-none"
                                />
                              )}
                              <div
                                className={`truncate px-2 py-1 text-xs ${
                                  isDir ? "font-medium text-amber-100" : "text-zinc-300"
                                }`}
                              >
                                {isDir ? `${entry.name}/` : entry.name}
                              </div>
                            </button>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                <div className="flex flex-wrap items-center justify-center gap-2">
                  <Button
                    className="rounded-md border border-zinc-700 px-3 py-1 text-sm disabled:opacity-40"
                    isDisabled={!data || data.page <= 1 || loading}
                    onPress={() => setPage(1)}
                  >
                    最初
                  </Button>
                  <Button
                    className="rounded-md border border-zinc-700 px-3 py-1 text-sm disabled:opacity-40"
                    isDisabled={!data || data.page <= 1 || loading}
                    onPress={() => setPage((value) => Math.max(1, value - 1))}
                  >
                    前へ
                  </Button>
                  <div className="flex items-center gap-1.5 text-sm text-zinc-400">
                    <Input
                      type="number"
                      inputMode="numeric"
                      min={1}
                      max={data?.page_count ?? 1}
                      value={pageInput}
                      onChange={(event) => setPageInput(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          goToPageInput();
                        }
                      }}
                      onBlur={goToPageInput}
                      disabled={!data || loading}
                      aria-label="ページ番号"
                      className="w-16 rounded-md border border-zinc-700 bg-zinc-900 px-2 py-1 text-center text-sm text-zinc-100 tabular-nums disabled:opacity-40 [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
                    />
                    <span className="tabular-nums">
                      / {data ? data.page_count : "—"}
                    </span>
                    <Button
                      className="rounded-md border border-zinc-700 px-2.5 py-1 text-sm disabled:opacity-40"
                      isDisabled={!data || loading}
                      onPress={goToPageInput}
                    >
                      移動
                    </Button>
                  </div>
                  <Button
                    className="rounded-md border border-zinc-700 px-3 py-1 text-sm disabled:opacity-40"
                    isDisabled={!data || data.page >= data.page_count || loading}
                    onPress={() => setPage((value) => value + 1)}
                  >
                    次へ
                  </Button>
                  <Button
                    className="rounded-md border border-zinc-700 px-3 py-1 text-sm disabled:opacity-40"
                    isDisabled={!data || data.page >= data.page_count || loading}
                    onPress={() => {
                      if (data) setPage(data.page_count);
                    }}
                  >
                    最後
                  </Button>
                </div>

                <div className="overflow-hidden rounded-md border border-zinc-800">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-zinc-900/80 text-xs text-zinc-400">
                      <tr>
                        <th className="w-10 px-2 py-2">
                          <span className="sr-only">選択</span>
                        </th>
                        <th className="px-3 py-2 font-medium">種類</th>
                        <th className="px-3 py-2 font-medium">名前</th>
                        <th className="px-3 py-2 font-medium">サイズ</th>
                        <th className="px-3 py-2 font-medium">更新日時</th>
                      </tr>
                    </thead>
                    <tbody>
                      {displayTable.map((entry) => {
                        const isChecked = checked.has(entry.path);
                        const isFocused = focused?.path === entry.path;
                        return (
                          <tr
                            key={entry.path}
                            {...{ [ASSET_PATH_ATTR]: entry.path }}
                            className={`cursor-pointer border-t border-zinc-800/80 ${
                              isChecked
                                ? "bg-sky-500/15"
                                : isFocused
                                  ? "bg-sky-500/10"
                                  : "hover:bg-zinc-900/70"
                            }`}
                            onPointerDown={(event) => beginDragSelect(entry, event)}
                            onClick={(event) => {
                              if (consumeSuppressedClick()) return;
                              if (event.shiftKey) selectRange(entry);
                              else if (event.metaKey || event.ctrlKey) toggleCheck(entry);
                              else {
                                focusEntry(entry);
                                if (!checked.has(entry.path)) toggleCheck(entry, true);
                              }
                            }}
                            onDoubleClick={() => void openFolder(entry)}
                          >
                            <td className="px-2 py-2" onClick={(event) => event.stopPropagation()}>
                              <input
                                type="checkbox"
                                className="h-3.5 w-3.5 accent-sky-500"
                                checked={isChecked}
                                onChange={(event) => toggleCheck(entry, event.target.checked)}
                              />
                            </td>
                            <td className="px-3 py-2 text-zinc-400">
                              {entry.is_directory ? "フォルダ" : entry.media_type}
                            </td>
                            <td className="px-3 py-2">
                              {entry.is_directory ? (
                                <button
                                  type="button"
                                  className="text-sky-300 hover:underline"
                                  data-no-drag-select
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    void openFolder(entry);
                                  }}
                                >
                                  {entry.name}
                                </button>
                              ) : (
                                entry.name
                              )}
                            </td>
                            <td className="px-3 py-2 text-zinc-400">
                              {entry.is_directory ? "—" : humanSize(entry.size)}
                            </td>
                            <td className="px-3 py-2 text-zinc-400">
                              {formatDate(entry.modified_at)}
                            </td>
                          </tr>
                        );
                      })}
                      {!displayTable.length && !loading && (
                        <tr>
                          <td colSpan={5} className="px-3 py-6 text-center text-zinc-500">
                            エントリがありません
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              <aside className="flex flex-col gap-4 rounded-lg border border-zinc-800 bg-zinc-950/60 p-4 lg:border-l">
                <div>
                  <h2 className="mb-2 text-sm font-semibold text-zinc-200">選択 / プレビュー</h2>
                  {checkedCount > 1 && (
                    <div className="mb-3 rounded-md border border-sky-900/50 bg-sky-950/30 p-3 text-sm text-sky-100">
                      <p className="font-medium">{checkedCount} 件選択中</p>
                      <p className="mt-1 text-xs text-sky-200/80">
                        一括削除は上のツールバーから。プレビュー・移動は最後にフォーカスした 1 件です。
                      </p>
                      <ul className="mt-2 max-h-28 overflow-auto text-xs text-sky-100/70">
                        {checkedList.slice(0, 30).map((entry) => (
                          <li key={entry.path} className="truncate">
                            {entry.name}
                          </li>
                        ))}
                        {checkedCount > 30 && <li>…ほか {checkedCount - 30} 件</li>}
                      </ul>
                    </div>
                  )}
                  {focused ? (
                    <div className="space-y-2 text-sm text-zinc-300">
                      <p className="break-all font-medium text-zinc-100">{focused.path}</p>
                      <p>
                        種類: {focused.kind} / サイズ:{" "}
                        {focused.is_directory ? "—" : humanSize(focused.size)}
                      </p>
                      <p>更新: {formatDate(focused.modified_at)}</p>

                      {(focused.media_type === "image" ||
                        focused.media_type === "video") && (
                        <div className="flex w-full items-center justify-center overflow-hidden rounded border border-zinc-800 bg-black/50">
                          {/*
                            Don't force w-full + short max-height: portrait video would
                            shrink to a tiny letterboxed strip. Cap by viewport height
                            and let intrinsic aspect ratio decide width.
                          */}
                          {focused.media_type === "image" ? (
                            <img
                              key={focused.path}
                              src={contentUrl(
                                focused.volume,
                                focused.path,
                                false,
                                focused,
                              )}
                              alt={focused.name}
                              loading="eager"
                              fetchPriority="high"
                              decoding="async"
                              className="max-h-[min(70vh,36rem)] max-w-full object-contain"
                            />
                          ) : (
                            <video
                              key={focused.path}
                              controls
                              playsInline
                              autoPlay={false}
                              preload="auto"
                              src={contentUrl(
                                focused.volume,
                                focused.path,
                                false,
                                focused,
                              )}
                              className="max-h-[min(70vh,36rem)] max-w-full object-contain"
                            />
                          )}
                        </div>
                      )}
                      {focused.media_type === "audio" && (
                        <audio
                          key={focused.path}
                          controls
                          preload="auto"
                          src={contentUrl(focused.volume, focused.path, false, focused)}
                          className="w-full"
                        />
                      )}

                      <div className="flex flex-wrap gap-2 pt-1">
                        {focused.is_directory ? (
                          <Button
                            className="rounded-md bg-sky-600 px-3 py-1.5 text-sm text-white hover:bg-sky-500"
                            onPress={() => void openFolder(focused)}
                          >
                            フォルダを開く
                          </Button>
                        ) : (
                          <a
                            className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm hover:bg-zinc-800"
                            href={contentUrl(focused.volume, focused.path, true, focused)}
                          >
                            ダウンロード
                          </a>
                        )}
                      </div>
                    </div>
                  ) : (
                    <p className="text-sm text-zinc-500">
                      チェックで複数選択、クリックでプレビュー対象を切り替え。
                    </p>
                  )}
                </div>

                {(checkedCount > 0 || focused) && (
                  <div className="space-y-2 border-t border-zinc-800 pt-3">
                    <h3 className="text-sm font-semibold">
                      {checkedCount > 1
                        ? `一括移動（${checkedCount}件）`
                        : "名前変更・移動（1件）"}
                    </h3>
                    {checkedCount > 1 && (
                      <p className="text-xs leading-relaxed text-zinc-500">
                        移動先フォルダを指定すると、各ファイルは元の名前のままその中へ移動します。
                        Inputs ↔ Outputs の Volume 間移動にも対応しています。
                      </p>
                    )}
                    <Select
                      selectedKey={destVolume}
                      onSelectionChange={(key) => setDestVolume(String(key) as VolumeId)}
                    >
                      <Label className="mb-1 block text-xs text-zinc-400">移動先 Volume</Label>
                      <Button className="flex w-full items-center justify-between rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-left text-sm">
                        <SelectValue />
                        <span aria-hidden>▾</span>
                      </Button>
                      <Popover className="rounded-md border border-zinc-700 bg-zinc-900 p-1 shadow-xl">
                        <ListBox className="outline-none">
                          {VOLUMES.map((option) => (
                            <ListBoxItem
                              key={option.id}
                              id={option.id}
                              className="cursor-pointer rounded px-3 py-1.5 text-sm outline-none data-[focused]:bg-zinc-800"
                            >
                              {option.label}
                            </ListBoxItem>
                          ))}
                        </ListBox>
                      </Popover>
                    </Select>
                    <TextField value={destPath} onChange={setDestPath}>
                      <Label className="mb-1 block text-xs text-zinc-400">
                        {checkedCount > 1 ? "移動先フォルダ" : "移動先パス"}
                      </Label>
                      <Input
                        className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm"
                        placeholder={
                          checkedCount > 1
                            ? "例: archive または video/done（空 = Volume 直下）"
                            : "例: folder/new-name.png"
                        }
                      />
                    </TextField>
                    <Checkbox
                      isSelected={moveOverwrite}
                      onChange={setMoveOverwrite}
                      className="flex items-center gap-2 text-sm text-zinc-300"
                    >
                      <div className="flex h-4 w-4 items-center justify-center rounded border border-zinc-600 data-[selected]:bg-sky-600">
                        {moveOverwrite ? "✓" : ""}
                      </div>
                      移動先を上書きする
                    </Checkbox>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        className="rounded-md bg-sky-600 px-3 py-1.5 text-sm text-white hover:bg-sky-500 disabled:opacity-50"
                        onPress={() => void handleMove()}
                        isDisabled={busy}
                      >
                        {checkedCount > 1
                          ? `${checkedCount}件を移動`
                          : "名前変更・移動を実行"}
                      </Button>
                    </div>
                  </div>
                )}

                <div className="space-y-2 border-t border-zinc-800 pt-3">
                  <h3 className="text-sm font-semibold">空フォルダを作成</h3>
                  <p className="text-xs leading-relaxed text-zinc-500">
                    現在のフォルダ
                    {path ? (
                      <>
                        （<span className="text-zinc-400">{path}</span>）
                      </>
                    ) : (
                      "（Volume 直下）"
                    )}
                    に作成します。ネストする場合は <code className="text-zinc-400">a/b</code>{" "}
                    のように入力できます。
                  </p>
                  <TextField value={newFolderName} onChange={setNewFolderName}>
                    <Label className="mb-1 block text-xs text-zinc-400">フォルダ名</Label>
                    <Input
                      className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm"
                      placeholder="例: archive または shots/take-01"
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          void handleCreateFolder();
                        }
                      }}
                    />
                  </TextField>
                  <Button
                    className="rounded-md bg-violet-700 px-3 py-1.5 text-sm text-white hover:bg-violet-600 disabled:opacity-50"
                    onPress={() => void handleCreateFolder()}
                    isDisabled={busy || !newFolderName.trim()}
                  >
                    フォルダを作成
                  </Button>
                </div>

                <div className="space-y-2 border-t border-zinc-800 pt-3">
                  <h3 className="text-sm font-semibold">ローカルファイルを追加</h3>
                  <FileTrigger
                    allowsMultiple
                    onSelect={(fileList) => {
                      setUploadFilesState(fileList ? Array.from(fileList) : null);
                    }}
                  >
                    <Button className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm hover:bg-zinc-800">
                      ファイルを選択
                    </Button>
                  </FileTrigger>
                  {uploadFilesState?.length ? (
                    <p className="text-xs text-zinc-400">
                      {uploadFilesState.length} 件選択中:{" "}
                      {uploadFilesState.map((file) => file.name).join(", ")}
                    </p>
                  ) : null}
                  <TextField value={uploadDest} onChange={setUploadDest}>
                    <Label className="mb-1 block text-xs text-zinc-400">保存先フォルダ</Label>
                    <Input className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
                  </TextField>
                  <Checkbox
                    isSelected={uploadOverwrite}
                    onChange={setUploadOverwrite}
                    className="flex items-center gap-2 text-sm text-zinc-300"
                  >
                    <div className="flex h-4 w-4 items-center justify-center rounded border border-zinc-600 data-[selected]:bg-sky-600">
                      {uploadOverwrite ? "✓" : ""}
                    </div>
                    同名を上書き
                  </Checkbox>
                  <Button
                    className="rounded-md bg-emerald-700 px-3 py-1.5 text-sm text-white hover:bg-emerald-600 disabled:opacity-50"
                    onPress={() => void handleUpload()}
                    isDisabled={busy}
                  >
                    アップロード
                  </Button>
                </div>

                {statusMessage && (
                  <p className="rounded-md border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-xs text-zinc-300">
                    {statusMessage}
                  </p>
                )}
              </aside>
            </div>
          </TabPanel>
        ))}
      </Tabs>
    </div>
  );
}
