export const MODEL_VOLUME = "comfy-model";
export const INPUT_VOLUME = "comfy-inputs";
export const OUTPUT_VOLUME = "comfy-outputs";

export const ALLOWED_VOLUMES = new Set([
  MODEL_VOLUME,
  INPUT_VOLUME,
  OUTPUT_VOLUME,
]);

export const TRANSFERABLE_VOLUMES = new Set([INPUT_VOLUME, OUTPUT_VOLUME]);

export const VOLUME_LABELS: Record<string, string> = {
  [INPUT_VOLUME]: "Inputs",
  [OUTPUT_VOLUME]: "Outputs",
  [MODEL_VOLUME]: "Models",
};

export const COMFY_MODEL_SUBDIRS = new Set([
  "checkpoints",
  "diffusion_models",
  "loras",
  "text_encoders",
  "audio_encoders",
  "clip",
  "clip_vision",
  "controlnet",
  "vae",
  "embeddings",
  "latent_upscale_models",
  "upscale_models",
  "detection",
]);

export const IMAGE_EXTENSIONS = new Set([
  ".bmp",
  ".gif",
  ".jpeg",
  ".jpg",
  ".png",
  ".webp",
]);
export const VIDEO_EXTENSIONS = new Set([".m4v", ".mov", ".mp4", ".webm"]);
export const AUDIO_EXTENSIONS = new Set([
  ".flac",
  ".m4a",
  ".mp3",
  ".ogg",
  ".wav",
]);

export const SORT_CHOICES = [
  "name_asc",
  "name_desc",
  "modified_desc",
  "modified_asc",
  "size_desc",
  "size_asc",
] as const;

export type SortMode = (typeof SORT_CHOICES)[number];

export type AssetKind = "file" | "directory" | "symlink";

export interface AssetEntry {
  volume: string;
  path: string;
  name: string;
  kind: AssetKind;
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

export interface ModalLsRow {
  Filename: string;
  Type: string;
  "Created/Modified"?: string;
  Size?: string;
}

export interface MaterializedFile {
  localPath: string;
  name: string;
  mediaType: string;
  size: number;
}
