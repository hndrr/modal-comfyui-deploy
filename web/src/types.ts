export type VolumeId = "comfy-inputs" | "comfy-outputs" | "comfy-model";

export type SortMode =
  | "name_asc"
  | "name_desc"
  | "modified_desc"
  | "modified_asc"
  | "size_desc"
  | "size_asc";

export interface AssetEntry {
  volume: string;
  path: string;
  name: string;
  kind: string;
  size: number;
  modified_at: string;
  media_type: string;
  is_directory: boolean;
}

export interface BreadcrumbItem {
  label: string;
  path: string;
}

export interface AssetListResponse {
  volume: string;
  path: string;
  breadcrumb: BreadcrumbItem[];
  page: number;
  page_size: number;
  page_count: number;
  total: number;
  image_total: number;
  status: string;
  entries: AssetEntry[];
}

export interface VolumeInfo {
  id: VolumeId;
  label: string;
}

export const SORT_OPTIONS: { id: SortMode; label: string }[] = [
  { id: "name_asc", label: "名前（昇順）" },
  { id: "name_desc", label: "名前（降順）" },
  { id: "modified_desc", label: "更新日時（新しい順）" },
  { id: "modified_asc", label: "更新日時（古い順）" },
  { id: "size_desc", label: "サイズ（大きい順）" },
  { id: "size_asc", label: "サイズ（小さい順）" },
];
