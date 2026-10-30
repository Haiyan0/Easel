/**
 * 原页面数据适配：local 继续调用原 lib/api；control 只经身份/CSRF 客户端
 * 调用 Task 5A 已接通的工作区路由。组件不感知 Cookie、路径或服务地址。
 */
import type {
  Idea, IdeaInput, OutputContent, OutputNode, PersonaFile, PersonaItem,
  ProfileBuildResponse, ProfileBuildStatus, ScheduleContext, ScheduleInput,
  ScheduleItem, SkillDetail, SkillItem, SkillResponse, TrendGroup,
  UploadedFile,
} from './api';
import {
  adoptOversize as localAdoptOversize,
  createIdea as localCreateIdea,
  createSchedule as localCreateSchedule,
  deleteIdea as localDeleteIdea,
  deleteOutput as localDeleteOutput,
  deletePersona as localDeletePersona,
  deleteSchedule as localDeleteSchedule,
  fetchArtifactArchive as localFetchArtifactArchive,
  buildProfile as localBuildProfile,
  executeSkill as localExecuteSkill,
  fetchIdeas as localFetchIdeas,
  fetchSkillDetail as localFetchSkillDetail,
  fetchSkills as localFetchSkills,
  fetchOutputContent as localFetchOutputContent,
  fetchOutputs as localFetchOutputs,
  fetchPersonaFiles as localFetchPersonaFiles,
  fetchPersonas as localFetchPersonas,
  fetchSchedule as localFetchSchedule,
  fetchScheduleContext as localFetchScheduleContext,
  fetchStatus as localFetchStatus,
  fetchTrends as localFetchTrends,
  mediaUrl as localMediaUrl,
  profileBuildStatus as localProfileBuildStatus,
  savePersonaFile as localSavePersonaFile,
  updateIdea as localUpdateIdea,
  updateSchedule as localUpdateSchedule,
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
  fetchTrends(
    platforms: string, limit?: number, signal?: AbortSignal,
  ): Promise<{
    trends: TrendGroup[]; updated: number; stale?: boolean;
    failed_platforms?: string[];
  }>;
  fetchSchedule(signal?: AbortSignal): Promise<ScheduleItem[]>;
  fetchScheduleContext(
    days?: number, signal?: AbortSignal,
  ): Promise<ScheduleContext>;
  createSchedule(
    item: ScheduleInput, signal?: AbortSignal,
  ): Promise<ScheduleItem>;
  updateSchedule(
    id: string, item: ScheduleInput, signal?: AbortSignal,
  ): Promise<ScheduleItem>;
  deleteSchedule(id: string, signal?: AbortSignal): Promise<{ ok: boolean }>;
  fetchIdeas(signal?: AbortSignal): Promise<Idea[]>;
  createIdea(item: IdeaInput, signal?: AbortSignal): Promise<Idea>;
  updateIdea(
    id: string, item: IdeaInput, signal?: AbortSignal,
  ): Promise<Idea>;
  deleteIdea(id: string, signal?: AbortSignal): Promise<{ ok: boolean }>;
  fetchSkills(signal?: AbortSignal): Promise<SkillItem[]>;
  fetchSkillDetail(name: string, signal?: AbortSignal): Promise<SkillDetail>;
  executeSkill(
    skill: string, input: string, persona?: string,
    signal?: AbortSignal, turnId?: string,
    onAdmission?: (runId: string, sessionId: string) => void,
  ): Promise<SkillResponse>;
  stopTask(runId: string, signal?: AbortSignal): Promise<void>;
  skillStatus(
    name: string, signal?: AbortSignal,
  ): Promise<{ state: string; response?: string; runId?: string }>;
  buildProfile(
    name: string, form: Record<string, unknown>, signal?: AbortSignal,
    turnId?: string,
  ): Promise<ProfileBuildResponse>;
  profileBuildStatus(
    name: string, signal?: AbortSignal,
  ): Promise<ProfileBuildStatus>;
  personasEnabled: boolean;
  outputsEnabled: boolean;
  workbenchEnabled: boolean;
  accountsEnabled: boolean;
  skillsEnabled: boolean;
  skillEnvEnabled: boolean;
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

function optionalString(row: Record<string, unknown>, key: string,
                       label: string): string | undefined {
  const value = row[key];
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'string') throw new Error(`${label}响应无效`);
  return value;
}

function trendResponse(value: unknown): {
  trends: TrendGroup[]; updated: number; stale?: boolean;
  failed_platforms?: string[];
} {
  const row = needObject(value, '热点响应无效');
  const trends = needArray(row.trends, '热点响应无效').map((item) => {
    const group = needObject(item, '热点响应无效');
    if (typeof group.platform !== 'string' || typeof group.label !== 'string') {
      throw new Error('热点响应无效');
    }
    const items = needArray(group.items, '热点响应无效').map((entry) => {
      const trend = needObject(entry, '热点响应无效');
      if (typeof trend.title !== 'string' || typeof trend.hot !== 'string'
          || typeof trend.url !== 'string') {
        throw new Error('热点响应无效');
      }
      return { title: trend.title, hot: trend.hot, url: trend.url };
    });
    const result: TrendGroup = {
      platform: group.platform, label: group.label, items,
      ...(typeof group.ok === 'boolean' ? { ok: group.ok } : {}),
    };
    if (group.stale === true) result.stale = true;
    if (typeof group.cached_at === 'number'
        && Number.isFinite(group.cached_at)) {
      result.cached_at = group.cached_at;
    }
    return result;
  });
  if (typeof row.updated !== 'number' || !Number.isFinite(row.updated)) {
    throw new Error('热点响应无效');
  }
  const result: {
    trends: TrendGroup[]; updated: number; stale?: boolean;
    failed_platforms?: string[];
  } = { trends, updated: row.updated };
  if (row.stale === true) result.stale = true;
  if (row.failed_platforms !== undefined) {
    result.failed_platforms = needArray(
      row.failed_platforms, '热点响应无效').map((item) => {
      if (typeof item !== 'string') throw new Error('热点响应无效');
      return item;
    });
  }
  return result;
}

function scheduleItem(value: unknown): ScheduleItem {
  const row = needObject(value, '日历响应无效');
  if (typeof row.id !== 'string' || typeof row.title !== 'string'
      || typeof row.date !== 'string') {
    throw new Error('日历响应无效');
  }
  const result: ScheduleItem = {
    id: row.id,
    title: row.title,
    date: row.date,
    platform: typeof row.platform === 'string' ? row.platform : '',
    time: typeof row.time === 'string' ? row.time : '',
    status: typeof row.status === 'string' ? row.status : 'idea',
    note: typeof row.note === 'string' ? row.note : '',
  };
  for (const key of ['kind', 'url', 'source', 'event_type', 'end_date'] as const) {
    const text = optionalString(row, key, '日历');
    if (text !== undefined) result[key] = text;
  }
  return result;
}

function scheduleList(value: unknown): ScheduleItem[] {
  return needArray(value, '日历响应无效').map(scheduleItem);
}

function scheduleContext(value: unknown): ScheduleContext {
  const row = needObject(value, '日历摘要响应无效');
  const result: ScheduleContext = {};
  if (row.today !== undefined) {
    if (typeof row.today !== 'string') throw new Error('日历摘要响应无效');
    result.today = row.today;
  }
  for (const key of ['window_days', 'published_recent'] as const) {
    const number = row[key];
    if (number === undefined) continue;
    if (typeof number !== 'number' || !Number.isFinite(number)) {
      throw new Error('日历摘要响应无效');
    }
    result[key] = number;
  }
  if (row.upcoming_schedules !== undefined) {
    result.upcoming_schedules = scheduleList(row.upcoming_schedules);
  }
  if (row.upcoming_events !== undefined) {
    result.upcoming_events = scheduleList(row.upcoming_events);
  }
  if (row.suggestions !== undefined) {
    result.suggestions = needArray(row.suggestions, '日历摘要响应无效').map(
      (item) => {
        if (typeof item !== 'string') throw new Error('日历摘要响应无效');
        return item;
      });
  }
  if (row.per_platform !== undefined) {
    result.per_platform = needArray(
      row.per_platform, '日历摘要响应无效').map((item) => {
      const platform = needObject(item, '日历摘要响应无效');
      if (typeof platform.platform !== 'string'
          || typeof platform.recent_count !== 'number') {
        throw new Error('日历摘要响应无效');
      }
      return {
        platform: platform.platform,
        recent_count: platform.recent_count,
        last_date: typeof platform.last_date === 'string'
          ? platform.last_date : null,
        days_since_last: typeof platform.days_since_last === 'number'
          ? platform.days_since_last : null,
      };
    });
  }
  return result;
}

function ideaItem(value: unknown): Idea {
  const row = needObject(value, '选题响应无效');
  if (typeof row.id !== 'string' || typeof row.title !== 'string') {
    throw new Error('选题响应无效');
  }
  return {
    id: row.id,
    title: row.title,
    note: typeof row.note === 'string' ? row.note : '',
    source: typeof row.source === 'string' ? row.source : '',
    status: typeof row.status === 'string' ? row.status : 'pending',
    created: typeof row.created === 'number' ? row.created : 0,
  };
}

function ideaList(value: unknown): Idea[] {
  return needArray(value, '选题响应无效').map(ideaItem);
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
  if (status !== null && status >= 500) {
    return `工作区服务暂不可用（${status}）。`;
  }
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
  runTask?(
    sessionId: string, runId: string, signal?: AbortSignal,
  ): Promise<{ state: string; response?: string }>;
}

/** control 适配器：所有调用都带身份 epoch/CSRF，错误保留 HTTP 语义。 */
function skillList(value: unknown): SkillItem[] {
  return needArray(value, '技能列表响应无效').map((item) => {
    const row = needObject(item, '技能列表响应无效');
    if (typeof row.name !== 'string' || !row.name
        || typeof row.description !== 'string'
        || typeof row.layer !== 'string'
        || typeof row.needsApi !== 'boolean'
        || typeof row.apiConfigured !== 'boolean') {
      throw new Error('技能列表响应无效');
    }
    return {
      name: row.name, description: row.description, layer: row.layer,
      needsApi: row.needsApi, apiConfigured: row.apiConfigured,
    };
  });
}

function skillDetail(value: unknown): SkillDetail {
  const row = needObject(value, '技能详情响应无效');
  if (typeof row.name !== 'string' || typeof row.layer !== 'string'
      || typeof row.description !== 'string' || typeof row.body !== 'string'
      || typeof row.needsApi !== 'boolean'
      || typeof row.apiConfigured !== 'boolean') {
    throw new Error('技能详情响应无效');
  }
  return {
    name: row.name, layer: row.layer, description: row.description,
    body: row.body, needsApi: row.needsApi,
    apiConfigured: row.apiConfigured,
    apiSpec: row.apiSpec && typeof row.apiSpec === 'object'
      ? row.apiSpec as SkillDetail['apiSpec'] : null,
  };
}

function profileBuild(value: unknown): ProfileBuildResponse {
  const row = needObject(value, '画像构建响应无效');
  if (typeof row.created !== 'boolean' || typeof row.name !== 'string') {
    throw new Error('画像构建响应无效');
  }
  return {
    created: row.created, name: row.name,
    ...(typeof row.async === 'boolean' ? { async: row.async } : {}),
    ...(typeof row.status === 'string' ? { status: row.status } : {}),
    ...(typeof row.log === 'string' ? { log: row.log } : {}),
    ...(typeof row.runId === 'string' ? { runId: row.runId } : {}),
    ...(typeof row.sessionId === 'string' ? { sessionId: row.sessionId } : {}),
    ...(typeof row.detail === 'string' ? { detail: row.detail } : {}),
    ...(row.enhancement && typeof row.enhancement === 'object'
      ? { enhancement: row.enhancement as { state: string; detail?: string } }
      : {}),
  };
}

function profileStatus(value: unknown): ProfileBuildStatus {
  const row = needObject(value, '画像增强状态响应无效');
  if (typeof row.state !== 'string' || typeof row.log !== 'string') {
    throw new Error('画像增强状态响应无效');
  }
  return { state: row.state as ProfileBuildStatus['state'], log: row.log };
}

function makeTaskId(): string {
  try {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return crypto.randomUUID();
    }
  } catch { /* fall through */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

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
    async fetchTrends(platforms, limit = 12, signal) {
      return trendResponse(await call<unknown>(
        `/api/trends?platforms=${encodeURIComponent(platforms)}&limit=${limit}`,
        { signal }));
    },
    async fetchSchedule(signal) {
      return scheduleList(await call<unknown>('/api/schedule', { signal }));
    },
    async fetchScheduleContext(days = 14, signal) {
      return scheduleContext(await call<unknown>(
        `/api/schedule/context?days=${days}`, { signal }));
    },
    async createSchedule(item, signal) {
      return scheduleItem(await call<unknown>('/api/schedule', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(item),
        signal,
      }));
    },
    async updateSchedule(id, item, signal) {
      return scheduleItem(await call<unknown>(
        `/api/schedule/${encodeURIComponent(id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(item),
          signal,
        }));
    },
    async deleteSchedule(id, signal) {
      const row = needObject(await call<unknown>(
        `/api/schedule/${encodeURIComponent(id)}`,
        { method: 'DELETE', signal }), '日历响应无效');
      return { ok: row.ok === true };
    },
    async fetchIdeas(signal) {
      return ideaList(await call<unknown>('/api/ideas', { signal }));
    },
    async createIdea(item, signal) {
      return ideaItem(await call<unknown>('/api/ideas', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(item),
        signal,
      }));
    },
    async updateIdea(id, item, signal) {
      return ideaItem(await call<unknown>(
        `/api/ideas/${encodeURIComponent(id)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(item),
          signal,
        }));
    },
    async deleteIdea(id, signal) {
      const row = needObject(await call<unknown>(
        `/api/ideas/${encodeURIComponent(id)}`,
        { method: 'DELETE', signal }), '选题响应无效');
      return { ok: row.ok === true };
    },
    async fetchSkills(signal) {
      return skillList(await call<unknown>('/api/skills', { signal }));
    },
    async fetchSkillDetail(name, signal) {
      return skillDetail(await call<unknown>(
        `/api/skill/${encodeURIComponent(name)}`, { signal }));
    },
    async executeSkill(skill, input, persona, signal, turnId, onAdmission) {
      const admission = await call<unknown>('/api/skill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          skill, input, turnId: turnId || makeTaskId(),
          ...(persona ? { persona } : {}),
        }),
        signal,
      });
      const row = needObject(admission, '技能执行接纳响应无效');
      if (typeof row.runId !== 'string' || typeof row.sessionId !== 'string'
          || !row.runId || !row.sessionId) {
        throw new Error('技能执行接纳响应无效');
      }
      if (!options.runTask) throw new Error('任务执行未装配');
      onAdmission?.(row.runId, row.sessionId);
      const result = await options.runTask(
        row.sessionId, row.runId, signal);
      if (result.state !== 'done') {
        throw new Error(`任务${result.state}`);
      }
      return { response: result.response || '' };
    },
    async stopTask(runId, signal) {
      await call<unknown>('/api/chat/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ runId }),
        signal,
      });
    },
    async skillStatus(name, signal) {
      const row = needObject(await call<unknown>(
        `/api/skill/status/${encodeURIComponent(name)}`, { signal }),
      '技能任务状态响应无效');
      if (typeof row.state !== 'string') {
        throw new Error('技能任务状态响应无效');
      }
      return {
        state: row.state,
        ...(typeof row.response === 'string' ? { response: row.response } : {}),
        ...(typeof row.runId === 'string' ? { runId: row.runId } : {}),
      };
    },
    async buildProfile(name, form, signal, turnId) {
      return profileBuild(await call<unknown>('/api/profile/build', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, form, turnId: turnId || makeTaskId() }),
        signal,
      }));
    },
    async profileBuildStatus(name, signal) {
      return profileStatus(await call<unknown>(
        `/api/profile/build/status/${encodeURIComponent(name)}`, { signal }));
    },
    personasEnabled: true,
    outputsEnabled: true,
    workbenchEnabled: true,
    accountsEnabled: false,
    skillsEnabled: true,
    skillEnvEnabled: false,
    attachmentsEnabled: true,
    personaBuildEnabled: true,
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
  fetchTrends: localFetchTrends,
  fetchSchedule: localFetchSchedule,
  fetchScheduleContext: localFetchScheduleContext,
  createSchedule: localCreateSchedule,
  updateSchedule: localUpdateSchedule,
  deleteSchedule: localDeleteSchedule,
  fetchIdeas: localFetchIdeas,
  createIdea: localCreateIdea,
  updateIdea: localUpdateIdea,
  deleteIdea: localDeleteIdea,
  fetchSkills: localFetchSkills,
  fetchSkillDetail: localFetchSkillDetail,
  executeSkill: localExecuteSkill,
  stopTask: async () => { throw new Error('当前模式不支持停止任务'); },
  skillStatus: async () => ({ state: 'unknown' }),
  buildProfile: localBuildProfile,
  profileBuildStatus: localProfileBuildStatus,
  personasEnabled: true,
  outputsEnabled: true,
  workbenchEnabled: true,
  accountsEnabled: true,
  skillsEnabled: true,
  skillEnvEnabled: true,
  attachmentsEnabled: true,
  personaBuildEnabled: true,
  wechatFetchEnabled: true,
  localSettingsEnabled: true,
  oversizeMode: 'adopt',
};
