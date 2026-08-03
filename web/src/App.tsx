import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
  deleteAsset,
  fetchAssets,
  moveAsset,
  thumbnailUrl,
  uploadFiles,
} from "./api/client";
import { formatDate, humanSize } from "./lib/format";
import type { AssetEntry, AssetListResponse, SortMode, VolumeId } from "./types";
import { SORT_OPTIONS } from "./types";

const VOLUMES: { id: VolumeId; label: string }[] = [
  { id: "comfy-inputs", label: "Inputs" },
  { id: "comfy-outputs", label: "Outputs" },
  { id: "comfy-model", label: "Models" },
];

export default function App() {
  const [volume, setVolume] = useState<VolumeId>("comfy-inputs");
  const [path, setPath] = useState("");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortMode>("name_asc");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<AssetListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<AssetEntry | null>(null);
  const [statusMessage, setStatusMessage] = useState("");
  const [destVolume, setDestVolume] = useState<VolumeId>("comfy-inputs");
  const [destPath, setDestPath] = useState("");
  const [moveOverwrite, setMoveOverwrite] = useState(false);
  const [uploadDest, setUploadDest] = useState("");
  const [uploadOverwrite, setUploadOverwrite] = useState(false);
  const [uploadFilesState, setUploadFilesState] = useState<File[] | null>(null);
  const [busy, setBusy] = useState(false);
  const requestId = useRef(0);

  const load = useCallback(async () => {
    const id = ++requestId.current;
    setLoading(true);
    setError(null);
    try {
      const result = await fetchAssets({
        volume,
        path,
        search,
        sort,
        page,
        pageSize: 48,
      });
      if (id !== requestId.current) return;
      setData(result);
      setPage(result.page);
    } catch (err) {
      if (id !== requestId.current) return;
      setError(err instanceof Error ? err.message : String(err));
      setData(null);
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [volume, path, search, sort, page]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    setUploadDest(path);
  }, [path]);

  useEffect(() => {
    if (selected) {
      setDestVolume(selected.volume as VolumeId);
      setDestPath(selected.path);
    } else {
      setDestPath("");
    }
  }, [selected]);

  // Inputs/Outputs: images in gallery, others in table. Models: all in table.
  const displayTable = useMemo(() => {
    if (volume === "comfy-model") return data?.entries ?? [];
    return (data?.entries ?? []).filter((entry) => entry.media_type !== "image");
  }, [data, volume]);

  const displayGallery = useMemo(() => {
    if (volume === "comfy-model") return [];
    return (data?.entries ?? []).filter((entry) => entry.media_type === "image");
  }, [data, volume]);

  function switchVolume(next: VolumeId) {
    setVolume(next);
    setPath("");
    setPage(1);
    setSearch("");
    setSearchInput("");
    setSelected(null);
    setDestVolume(next);
    setUploadDest("");
    setStatusMessage("");
    setError(null);
  }

  async function navigateTo(nextPath: string) {
    setPath(nextPath);
    setPage(1);
    setSelected(null);
  }

  async function openFolder(entry: AssetEntry) {
    if (!entry.is_directory) return;
    await navigateTo(entry.path);
  }

  async function handleUpload() {
    if (!uploadFilesState?.length) {
      setStatusMessage("アップロードするファイルを選択してください。");
      return;
    }
    setBusy(true);
    try {
      const result = await uploadFiles({
        volume,
        destination: uploadDest,
        overwrite: uploadOverwrite,
        files: uploadFilesState,
      });
      setStatusMessage(result.message);
      setUploadFilesState(null);
      await load();
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleMove() {
    if (!selected) return;
    setBusy(true);
    try {
      const result = await moveAsset({
        source_volume: selected.volume,
        source_path: selected.path,
        destination_volume: destVolume,
        destination_path: destPath,
        overwrite: moveOverwrite,
      });
      setStatusMessage(result.message);
      setSelected(null);
      await load();
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (!selected) return;
    setBusy(true);
    try {
      const result = await deleteAsset({
        volume: selected.volume,
        path: selected.path,
        recursive: selected.is_directory,
      });
      setStatusMessage(result.message);
      setSelected(null);
      await load();
    } catch (err) {
      setStatusMessage(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-7xl flex-col gap-4 px-4 py-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">
          Modal ComfyUI Asset Manager
        </h1>
        <p className="text-sm text-zinc-400">
          React UI · Hono API · <code className="text-zinc-300">modal volume</code> CLI.
          削除は完全削除です。一覧はメタデータのみ取得し、画像は遅延ロードします。
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
                    onPress={() => void load()}
                  >
                    更新
                  </Button>
                </div>

                <div className="flex flex-wrap gap-3">
                  <SearchField
                    value={searchInput}
                    onChange={setSearchInput}
                    onSubmit={() => {
                      setPage(1);
                      setSearch(searchInput);
                    }}
                    className="min-w-[16rem] flex-1"
                  >
                    <Label className="mb-1 block text-xs text-zinc-400">検索</Label>
                    <Input
                      className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm"
                      placeholder="現在のフォルダを検索（Enter）"
                    />
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

                <div className="flex items-center gap-3 text-sm text-zinc-400">
                  <span>{data?.status ?? "—"}</span>
                  {loading && (
                    <ProgressBar isIndeterminate aria-label="読み込み中" className="text-sky-300">
                      {({ percentage }) => (
                        <span className="text-xs">
                          読み込み中…{percentage != null ? ` ${Math.round(percentage)}%` : ""}
                        </span>
                      )}
                    </ProgressBar>
                  )}
                </div>

                {error && (
                  <div className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-sm text-red-200">
                    <p>{error}</p>
                    <Button
                      className="mt-2 rounded border border-red-800 px-2 py-1 text-xs"
                      onPress={() => void load()}
                    >
                      再試行
                    </Button>
                  </div>
                )}

                {displayGallery.length > 0 && (
                  <div>
                    <h2 className="mb-2 text-sm font-medium text-zinc-300">画像</h2>
                    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
                      {displayGallery.map((entry) => (
                        <button
                          key={entry.path}
                          type="button"
                          onClick={() => setSelected(entry)}
                          className={`overflow-hidden rounded-md border bg-zinc-900 text-left ${
                            selected?.path === entry.path
                              ? "border-sky-500"
                              : "border-zinc-800 hover:border-zinc-600"
                          }`}
                        >
                          <img
                            src={thumbnailUrl(entry.volume, entry.path, {
                              name: entry.name,
                              size: entry.size,
                              modified_at: entry.modified_at,
                              media_type: entry.media_type,
                            })}
                            alt={entry.name}
                            loading="lazy"
                            decoding="async"
                            className="aspect-square w-full object-contain bg-black/40"
                          />
                          <div className="truncate px-2 py-1 text-xs text-zinc-300">
                            {entry.name}
                          </div>
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                <div className="flex items-center justify-center gap-3">
                  <Button
                    className="rounded-md border border-zinc-700 px-3 py-1 text-sm disabled:opacity-40"
                    isDisabled={!data || data.page <= 1 || loading}
                    onPress={() => setPage((value) => Math.max(1, value - 1))}
                  >
                    前へ
                  </Button>
                  <span className="text-sm text-zinc-400">
                    {data ? `${data.page} / ${data.page_count}` : "—"}
                  </span>
                  <Button
                    className="rounded-md border border-zinc-700 px-3 py-1 text-sm disabled:opacity-40"
                    isDisabled={!data || data.page >= data.page_count || loading}
                    onPress={() => setPage((value) => value + 1)}
                  >
                    次へ
                  </Button>
                </div>

                <div className="overflow-hidden rounded-md border border-zinc-800">
                  <table className="w-full text-left text-sm">
                    <thead className="bg-zinc-900/80 text-xs text-zinc-400">
                      <tr>
                        <th className="px-3 py-2 font-medium">種類</th>
                        <th className="px-3 py-2 font-medium">名前</th>
                        <th className="px-3 py-2 font-medium">サイズ</th>
                        <th className="px-3 py-2 font-medium">更新日時</th>
                      </tr>
                    </thead>
                    <tbody>
                      {displayTable.map((entry) => (
                        <tr
                          key={entry.path}
                          className={`cursor-pointer border-t border-zinc-800/80 ${
                            selected?.path === entry.path
                              ? "bg-sky-500/10"
                              : "hover:bg-zinc-900/70"
                          }`}
                          onClick={() => setSelected(entry)}
                          onDoubleClick={() => void openFolder(entry)}
                        >
                          <td className="px-3 py-2 text-zinc-400">
                            {entry.is_directory ? "フォルダ" : entry.media_type}
                          </td>
                          <td className="px-3 py-2">
                            {entry.is_directory ? (
                              <button
                                type="button"
                                className="text-sky-300 hover:underline"
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
                      ))}
                      {!displayTable.length && !loading && (
                        <tr>
                          <td colSpan={4} className="px-3 py-6 text-center text-zinc-500">
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
                  <h2 className="mb-2 text-sm font-semibold text-zinc-200">選択した資産</h2>
                  {selected ? (
                    <div className="space-y-2 text-sm text-zinc-300">
                      <p className="break-all font-medium text-zinc-100">{selected.path}</p>
                      <p>
                        種類: {selected.kind} / サイズ:{" "}
                        {selected.is_directory ? "—" : humanSize(selected.size)}
                      </p>
                      <p>更新: {formatDate(selected.modified_at)}</p>

                      {selected.media_type === "image" && (
                        <img
                          src={contentUrl(selected.volume, selected.path, false, selected)}
                          alt={selected.name}
                          className="max-h-56 w-full rounded border border-zinc-800 object-contain"
                        />
                      )}
                      {selected.media_type === "video" && (
                        <video
                          controls
                          src={contentUrl(selected.volume, selected.path, false, selected)}
                          className="max-h-56 w-full rounded border border-zinc-800"
                        />
                      )}
                      {selected.media_type === "audio" && (
                        <audio
                          controls
                          src={contentUrl(selected.volume, selected.path, false, selected)}
                          className="w-full"
                        />
                      )}

                      <div className="flex flex-wrap gap-2 pt-1">
                        {selected.is_directory ? (
                          <Button
                            className="rounded-md bg-sky-600 px-3 py-1.5 text-sm text-white hover:bg-sky-500"
                            onPress={() => void openFolder(selected)}
                          >
                            フォルダを開く
                          </Button>
                        ) : (
                          <a
                            className="rounded-md border border-zinc-700 px-3 py-1.5 text-sm hover:bg-zinc-800"
                            href={contentUrl(selected.volume, selected.path, true, selected)}
                          >
                            ダウンロード
                          </a>
                        )}
                      </div>
                    </div>
                  ) : (
                    <p className="text-sm text-zinc-500">資産を選択してください。</p>
                  )}
                </div>

                {selected && (
                  <div className="space-y-2 border-t border-zinc-800 pt-3">
                    <h3 className="text-sm font-semibold">名前変更・移動</h3>
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
                      <Label className="mb-1 block text-xs text-zinc-400">移動先パス</Label>
                      <Input className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm" />
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
                        名前変更・移動を実行
                      </Button>
                      <DialogTrigger>
                        <Button
                          className="rounded-md bg-red-700 px-3 py-1.5 text-sm text-white hover:bg-red-600 disabled:opacity-50"
                          isDisabled={busy}
                        >
                          削除の確認へ
                        </Button>
                        <ModalOverlay className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
                          <Modal className="w-full max-w-md rounded-lg border border-zinc-700 bg-zinc-950 p-4 shadow-2xl">
                            <Dialog className="outline-none">
                              {({ close }) => (
                                <div className="space-y-3">
                                  <Heading
                                    slot="title"
                                    className="text-lg font-semibold text-red-200"
                                  >
                                    完全削除の確認
                                  </Heading>
                                  <p className="text-sm text-zinc-300">
                                    `{selected.volume}:{selected.path}` を完全に削除します。
                                    {selected.is_directory
                                      ? " 配下を含めて再帰的に削除します。"
                                      : ""}
                                    この操作は取り消せません。
                                  </p>
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
                                        void handleDelete();
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
                    </div>
                  </div>
                )}

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
