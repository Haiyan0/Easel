/**
 * 原页面数据适配：local 继续调用原 lib/api；control 只经身份/CSRF 客户端
 * 调用 Task 5A 已接通的工作区路由。组件不感知 Cookie、路径或服务地址。
 */
import type {
  OutputContent, OutputNode, PersonaFile, PersonaItem, UploadedFile,
} from './api';
import {
  adoptOversize as localAdoptOversize,
  deleteOutput as localDeleteOutput,
  deletePersona as localDeletePersona,
  fetchArtifactArchive as localFetchArtifactArchive,
  fetchOutputContent as localFetchOutputContent,
  fetchOutputs as localFetchOutputs,
  fetchPersonaFiles as localFetchPersonaFiles,
  fetchPersonas as localFetchPersonas,
  fetchStatus as localFetchStatus,
  mediaUrl as localMediaUrl,
  savePersonaFile as localSavePersonaFile,
  uploadFiles as localUploadFiles,
} from './api';

export type OversizeMode = 'adopt' | 'reject';

export interface WorkspaceStatus {
  state: 'not_started' | 'starting' | 'stopping' | 'ready' | 'unavailable';
  gateway: boolean;
  detail: string;
  skills: unknown[];
  personas: PersonaItem[];
  transport?: string;
  runtime?: string;
}

export interface UploadLimits { max_mb: number }

export interface WorkspaceData {
  fetchStatus(): Promise<WorkspaceStatus>;
  fetchPersonas(): Promise<PersonaItem[]>;
  fetchPersonaFiles(name: string): Promise<{ name: string; files: PersonaFile[] }>;
  savePersonaFile(name: string, filename: string, content: string): Promise<{ ok: boolean }>;
  deletePersona(name: string): Promise<{ ok: boolean; deleted: string }>;
  fetchOutputs(): Promise<OutputNode[]>;
  fetchOutputContent(path: string): Promise<OutputContent>;
  fetchArtifactArchive(path: string, signal?: AbortSignal): Promise<{ paths: string[] }>;
  deleteOutput(path: string): Promise<{ ok: boolean; deleted: string }>;
  mediaUrl(path: string): string;
  fetchText(url: string, signal?: AbortSignal): Promise<string>;
  uploadLimits(): Promise<UploadLimits>;
  uploadFiles(files: File[], sessionId: string): Promise<UploadedFile[]>;
  adoptOversize?: (files: File[], sessionId: string) => Promise<UploadedFile[]>;
  personasEnabled: boolean;
  outputsEnabled: boolean;
  attachmentsEnabled: boolean;
  personaBuildEnabled: boolean;
  wechatFetchEnabled: boolean;
  localSettingsEnabled: boolean;
  oversizeMode: OversizeMode;
}

function needObject(value: unknown, message: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(message);
  }
  return value as Record<string, unknown>;
}

function needArray(value: unknown, message: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(message);
  return value;
}

function personaList(value: unknown): PersonaItem[] {
  return needArray(value, '画像列表响应无效').map((item) => {
    const row = needObject(item, '画像列表响应无效');
    if (typeof row.name !== 'string' || !row.name) {
      throw new Error('画像列表响应无效');
    }
    return {
      name: row.name,
      description: typeof row.description === 'string' ? row.description : '',
    };
  });
}

function personaFiles(value: unknown): { name: string; files: PersonaFile[] } {
  const row = needObject(value, '画像文件响应无效');
  if (typeof row.name !== 'string') throw new Error('画像文件响应无效');
  const files = needArray(row.files, '画像文件响应无效').map((item) => {
    const file = needObject(item, '画像文件响应无效');
    if (typeof file.filename !== 'string' || typeof file.content !== 'string') {
      throw new Error('画像文件响应无效');
    }
    return { filename: file.filename, content: file.content };
  });
  return { name: row.name, files };
}

function outputTree(value: unknown): OutputNode[] {
  const rows = needArray(value, '内容库响应无效');
  const parse = (item: unknown, depth: number): OutputNode => {
    if (depth > 64) throw new Error('内容库响应无效');
    const row = needObject(item, '内容库响应无效');
    if (typeof row.name !== 'string' || typeof row.path !== 'string'
        || (row.type !== 'dir' && row.type !== 'file')) {
      throw new Error('内容库响应无效');
    }
    const node: OutputNode = {
      name: row.name, path: row.path, type: row.type,
    };
    if (typeof row.mtime === 'number') node.mtime = row.mtime;
    if (typeof row.size === 'number') node.size = row.size;
    if (typeof row.fileCount === 'number') node.fileCount = row.fileCount;
    if (row.kind === 'text' || row.kind === 'image' || row.kind === 'video'
        || row.kind === 'audio' || row.kind === 'binary') {
      node.kind = row.kind;
    }
    if (row.meta && typeof row.meta === 'object') {
      node.meta = row.meta as OutputNode['meta'];
    }
    if (row.children !== undefined) {
      node.children = needArray(row.children, '内容库响应无效')
        .map((child) => parse(child, depth + 1));
    }
    return node;
  };
  return rows.map((item) => parse(item, 0));
}

function outputContent(value: unknown): OutputContent {
  const row = needObject(value, '产物内容响应无效');
  if (typeof row.path !== 'string' || typeof row.content !== 'string'
      || typeof row.isBinary !== 'boolean'
      || !['text', 'image', 'video', 'audio', 'binary'].includes(String(row.kind))) {
    throw new Error('产物内容响应无效');
  }
  return {
    path: row.path, content: row.content,
    kind: row.kind as OutputContent['kind'], isBinary: row.isBinary,
  };
}

function archivePaths(value: unknown): { paths: string[] } {
  const row = needObject(value, '归档索引响应无效');
  return {
    paths: needArray(row.paths, '归档索引响应无效').filter(
      (path): path is string => typeof path === 'string'),
  };
}

function uploadList(value: unknown): UploadedFile[] {
  const row = needObject(value, '上传响应无效');
  return needArray(row.files, '上传响应无效').map((item) => {
    const file = needObject(item, '上传响应无效');
    if (typeof file.id !== 'string' || typeof file.name !== 'string'
        || typeof file.path !== 'string') {
      throw new Error('上传响应无效');
    }
    return { id: file.id, name: file.name, path: file.path };
  });
}

function statusView(value: unknown): WorkspaceStatus {
  const row = needObject(value, '工作区状态响应无效');
  const states = new Set([
    'not_started', 'starting', 'stopping', 'ready', 'unavailable',
  ]);
  const state = typeof row.state === 'string' && states.has(row.state)
    ? row.state as WorkspaceStatus['state'] : 'unavailable';
  return {
    state,
    gateway: row.gateway === true,
    detail: typeof row.detail === 'string' ? row.detail : '',
    skills: Array.isArray(row.skills) ? row.skills : [],
    personas: Array.isArray(row.personas) ? personaList(row.personas) : [],
    ...(typeof row.transport === 'string' ? { transport: row.transport } : {}),
    ...(typeof row.runtime === 'string' ? { runtime: row.runtime } : {}),
  };
}

function statusOf(error: unknown): number | null {
  if (!error || typeof error !== 'object') return null;
  const status = (error as { status?: unknown }).status;
  return typeof status === 'number' ? status : null;
}

export function workspaceErrorMessage(error: unknown, fallback: string): string {
  const status = statusOf(error);
  if (status === 401) return '登录已失效，请重新登录。';
  if (status === 403) return '无权访问该内容（403）。';
  if (status === 404) return '内容不存在或已被删除（404）。';
  if (status === 409) return '请求冲突或版本已变化（409）。';
  if (status === 413) return '文件超过上传上限（413）。';
  if (status === 429) return '操作过于频繁，请稍后重试（429）。';
  if (status === 503) return '工作区服务暂不可用（503）。';
  if (status === 0) return '网络不可用，请检查连接。';
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

export function partitionUploads(
  files: ArrayLike<{ size: number }>, maxMb: number,
): { accepted: Array<{ size: number }>; oversize: Array<{ size: number }> } {
  const cap = Math.max(0, maxMb) * 1024 * 1024;
  const accepted: Array<{ size: number }> = [];
  const oversize: Array<{ size: number }> = [];
  for (const file of Array.from(files)) {
    (file.size > cap ? oversize : accepted).push(file);
  }
  return { accepted, oversize };
}

export function oversizeUploadMessage(maxMb: number, mode: OversizeMode): string {
  return mode === 'reject'
    ? `单个文件超过 ${maxMb}MB 上传上限；多人工作区不支持本地复制通道，请压缩或拆分后再上传。`
    : `超过 ${maxMb}MB 的文件将复制为本地素材（不走上传）。`;
}

export interface ControlWorkspaceOptions {
  call<T>(path: string, init?: RequestInit): Promise<T>;
  fetchText(url: string, signal?: AbortSignal): Promise<string>;
}

/** control 适配器：所有调用都带身份 epoch/CSRF，错误保留 HTTP 语义。 */
export function createControlWorkspaceData(
  options: ControlWorkspaceOptions,
): WorkspaceData {
  const call = options.call;
  return {
    async fetchStatus() {
      return statusView(await call<unknown>('/api/status'));
    },
    async fetchPersonas() {
      return personaList(await call<unknown>('/api/personas'));
    },
    async fetchPersonaFiles(name) {
      return personaFiles(await call<unknown>(
        `/api/persona/${encodeURIComponent(name)}/files`));
    },
    async savePersonaFile(name, filename, content) {
      const payload = await call<unknown>(
        `/api/persona/${encodeURIComponent(name)}/file`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename, content }),
        });
      const row = needObject(payload, '画像保存响应无效');
      return { ok: row.ok === true };
    },
    async deletePersona(name) {
      const payload = await call<unknown>(
        `/api/persona/${encodeURIComponent(name)}`, { method: 'DELETE' });
      const row = needObject(payload, '画像删除响应无效');
      return {
        ok: row.ok === true,
        deleted: typeof row.deleted === 'string' ? row.deleted : name,
      };
    },
    async fetchOutputs() {
      return outputTree(await call<unknown>('/api/outputs'));
    },
    async fetchOutputContent(path) {
      return outputContent(await call<unknown>(
        `/api/output/${path.split('/').map(encodeURIComponent).join('/')}`));
    },
    async fetchArtifactArchive(path, signal) {
      return archivePaths(await call<unknown>(
        `/api/artifacts/archive?path=${encodeURIComponent(path)}`, { signal }));
    },
    async deleteOutput(path) {
      const payload = await call<unknown>(
        `/api/output/${path.split('/').map(encodeURIComponent).join('/')}`,
        { method: 'DELETE' });
      const row = needObject(payload, '内容删除响应无效');
      return {
        ok: row.ok === true,
        deleted: typeof row.deleted === 'string' ? row.deleted : path,
      };
    },
    mediaUrl: localMediaUrl,
    fetchText: options.fetchText,
    async uploadLimits() {
      const row = needObject(
        await call<unknown>('/api/upload/limits'), '上传上限响应无效');
      if (typeof row.max_mb !== 'number' || !Number.isFinite(row.max_mb)
          || row.max_mb <= 0) {
        throw new Error('上传上限响应无效');
      }
      return { max_mb: row.max_mb };
    },
    async uploadFiles(files, sessionId) {
      const form = new FormData();
      for (const file of files) form.append('files', file);
      form.append('sessionId', sessionId);
      return uploadList(await call<unknown>('/api/upload', {
        method: 'POST', body: form,
      }));
    },
    personasEnabled: true,
    outputsEnabled: true,
    attachmentsEnabled: true,
    personaBuildEnabled: false,
    wechatFetchEnabled: false,
    localSettingsEnabled: false,
    oversizeMode: 'reject',
  };
}

/** local 适配器：保持原单机行为，不改原 API/缓存语义。 */
export const localWorkspaceData: WorkspaceData = {
  async fetchStatus() {
    const value = await localFetchStatus();
    return {
      state: 'ready', gateway: value.gateway === true,
      detail: '', skills: value.skills || [],
      personas: value.personas || [],
      ...(value.transport ? { transport: value.transport } : {}),
      ...(value.runtime ? { runtime: value.runtime } : {}),
    };
  },
  fetchPersonas: localFetchPersonas,
  fetchPersonaFiles: localFetchPersonaFiles,
  savePersonaFile: localSavePersonaFile,
  deletePersona: localDeletePersona,
  fetchOutputs: localFetchOutputs,
  fetchOutputContent: localFetchOutputContent,
  fetchArtifactArchive: localFetchArtifactArchive,
  deleteOutput: localDeleteOutput,
  mediaUrl: localMediaUrl,
  fetchText: async (url, signal) => {
    const response = await fetch(url, { signal, cache: 'no-store' });
    if (!response.ok) throw new Error(`请求失败（${response.status}）`);
    return response.text();
  },
  uploadLimits: async () => {
    const response = await fetch('/api/upload/limits', { cache: 'no-store' });
    if (!response.ok) throw new Error(`请求失败（${response.status}）`);
    const row = needObject(await response.json(), '上传上限响应无效');
    return { max_mb: typeof row.max_mb === 'number' ? row.max_mb : 50 };
  },
  uploadFiles: localUploadFiles,
  adoptOversize: localAdoptOversize,
  personasEnabled: true,
  outputsEnabled: true,
  attachmentsEnabled: true,
  personaBuildEnabled: true,
  wechatFetchEnabled: true,
  localSettingsEnabled: true,
  oversizeMode: 'adopt',
};
