export const ALLOWED_IMPORTS: string[];
export const ALLOWED_SOURCES: RegExp[];
export function checkChunk(chunk: { imports: string[]; dynamicImports: string[]; moduleIds: string[] }): string[];
export function checkCode(code: string): string[];
