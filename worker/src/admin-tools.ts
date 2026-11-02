import {z} from "zod";

const request_id=z.string().regex(/^[a-zA-Z0-9_-]{1,96}$/).describe("Unique operation key. Reuse with identical arguments after disconnect.");
const candidate=z.string().min(1).max(200).describe("Exact candidate version returned by get_split_status or prepare_environment.");
const path=z.string().min(1).max(512).describe("Relative installed model path, e.g. loras/example.safetensors.");
const node=z.string().regex(/^[a-z0-9][a-z0-9_-]{0,99}$/);
export const adminTools={
  get_split_status:{description:"Read active and candidate environment, GPU count, pending work and Manager queue status. Does not wake a GPU.",inputs:{},write:false},
  list_nodes:{description:"Search node classes in the active split environment. Returns a bounded page of installed node definitions, not registry search results.",inputs:{query:z.string().max(120).optional(),offset:z.number().int().nonnegative().optional(),limit:z.number().int().min(1).max(100).optional()},write:false},
  prepare_environment:{description:"Prepare a CPU candidate copy of the specified active environment for custom-node changes; does not change the active version.",inputs:{active:z.string().min(1).max(200),request_id},write:true},
  change_node:{description:"Install a registry release or update an installed node pack in the specified candidate. Does not apply it to production. Check the operation and Manager queue before applying.",inputs:{candidate,action:z.enum(["install","update"]),node,version:z.string().regex(/^\d+\.\d+\.\d+$/).optional(),request_id},write:true},
  apply_environment:{description:"Validate the specified candidate, including on GPU, then switch the active version only on success.",inputs:{candidate,request_id},write:true},
  discard_environment:{description:"Discard only the specified inactive candidate, without touching the active environment.",inputs:{candidate,request_id},write:true},
  inspect_model:{description:"Read file size and SHA-256 before removal. For large files poll the returned request_id with get_admin_operation.",inputs:{path,request_id:request_id.optional()},write:false},
  add_model:{description:"Import a new model from a Hugging Face repository into one model category on a CPU worker. Never overwrite. Use get_admin_operation to track it.",inputs:{repo:z.string().max(160),file:z.string().max(512),revision:z.string().max(120).optional(),category:z.string().max(80),sha256:z.string().regex(/^[a-f0-9]{64}$/).optional(),request_id},write:true},
  remove_model:{description:"Remove one reviewed model from its loader category into private trash. Requires the current version token; cannot recursively delete.",inputs:{path,version:z.string().regex(/^[a-f0-9]{64}$/),request_id},write:true},
  restore_model:{description:"Restore the model removed by one remove_model operation. Never replace a new file.",inputs:{removal_id:request_id,request_id},write:true},
  get_admin_operation:{description:"Check a previously requested admin operation by its request_id without repeating it.",inputs:{request_id},write:false},
};
