import { z } from "zod";
import {libraryName, workflowFields} from "../../mcp/library.mjs";
export const filename = z.string().refine(libraryName, "Expected a relative .json workflow library name");
const strings = z.array(z.string().max(512)).max(120);
export const workflowInputs = {
  action:z.enum(Object.keys(workflowFields) as [keyof typeof workflowFields,...(keyof typeof workflowFields)[]]),
  filename:filename.optional(),format:z.enum(["api","ui","raw"]).optional(),
  view:z.enum(["summary","overview","detail","list","flat","health"]).optional(),section:z.string().max(200).optional(),
  groups:z.union([strings,z.string().max(512)]).optional(),types:strings.optional(),title:z.string().max(256).optional(),
  where:strings.optional(),ids:strings.optional(),upstream_of:z.string().optional(),downstream_of:z.string().optional(),
  depth:z.number().int().nonnegative().optional(),fields:z.enum(["ids","compact","detail"]).optional(),
  group_by:z.literal("type").optional(),limit:z.number().int().min(1).optional(),max_chars:z.number().int().positive().max(64000).optional(),
};
const id=z.uuid(), index=z.number().int().min(0);
const pager={offset:index.optional(),limit:z.number().int().min(1).max(100).optional()};
const kind=z.enum(["image","audio","video"]);
export const production = {
  get_job_workflow:{description:"Read the exact submitted API graph by split prompt_id. Edit the graph and use enqueue_workflow with a new request_id to generate a variation.",inputs:{prompt_id:id},write:false},
  list_outputs:{description:"List durable image/audio/video/3d outputs of one split job. Use GLB/SPZ filename and subfolder with get_3d; use the index for inspection or supported media reuse.",inputs:{prompt_id:id,...pager},write:false},
  reuse_output:{description:"Copy one completed image/audio/video output into loader-visible inputs on the split CPU. Select its index from list_outputs. 3D reuse is not supported. No regeneration or client download.",inputs:{prompt_id:id,index},write:true},
  list_inputs:{description:"List existing loader inputs by filename, kind and page. Does not include unfinished uploads.",inputs:{kind:kind.optional(),query:z.string().max(120).optional(),...pager},write:false},
  inspect_media:{description:"Read dimensions, duration, frame rate and audio stream information for an input filename or a job output (prompt_id and index). GLB/SPZ return format and file size only.",inputs:{filename:z.string().max(512).optional(),prompt_id:id.optional(),index:index.optional()},write:false},
};
