export const workflowFields: Record<"list"|"get"|"analyze"|"query"|"strip"|"slice", string[]>;
export function libraryName(value:unknown): value is string;
export function libraryRequest(args:Record<string,unknown>): boolean;
