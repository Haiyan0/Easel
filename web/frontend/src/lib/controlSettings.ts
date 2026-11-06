/**
 * control 个人设置/管理员默认配置的协议类型与表单转换。
 *
 * 本模块只做纯转换：所有服务器响应先校验形状；表单从不读取已保存的
 * 凭证，提交时只在本次内存表单中携带新输入的 secret。
 */

export const PERSONAL_CHANNELS = ['speech', 'image', 'video', 'music'] as const;
export const ADMIN_CHANNELS = [...PERSONAL_CHANNELS, 'chat'] as const;
export type SettingsChannel = typeof PERSONAL_CHANNELS[number];
export type AdminChannel = typeof ADMIN_CHANNELS[number];

export type RouteKind = 'complete' | 'managed-model';
export type ChannelSource = 'none' | 'admin' | 'user';
export type ChannelMode = 'none' | 'complete' | 'managed-model' | 'inherited';

export interface ChannelSettings {
  configured: boolean;
  source: ChannelSource;
  version: number;
  mode: ChannelMode;
  provider: string | null;
  model: string | null;
  base_url: string | null;
  protocol: string | null;
  options: Record<string, unknown>;
  allowed_models?: string[];
  personal_model?: string | null;
  personal_options?: Record<string, unknown>;
  broker_adapter: string | null;
  broker_supported: boolean;
  override_allowed: boolean;
}

export interface ProviderKeySchema {
  env: string;
  label: string;
  required: boolean;
  secret: boolean;
  choices: string[];
}

export interface ProviderSchema {
  name: string;
  keys: ProviderKeySchema[];
  override_allowed: boolean;
  broker_adapter: string | null;
}

export type ProviderCatalog =
  Partial<Record<AdminChannel, Record<string, ProviderSchema>>>;

export interface ModelSettings {
  version: number;
  channels: Record<AdminChannel, ChannelSettings>;
  providers: ProviderCatalog;
}

export interface HarnessSummary {
  locked: boolean;
  configured: boolean;
  runtime: string | null;
  revision: number | null;
  model: string | null;
  sandbox: string | null;
  timeout: number | null;
  codexAutoApprove: boolean | null;
  unavailableReason: string | null;
}

export interface ManagedChannelSettings {
  provider: string;
  model: string;
  base_url: string;
  protocol: string;
  options: Record<string, unknown>;
  allowed_models?: string[];
  credential_configured: boolean;
}

export interface ManagedSettings {
  version: number;
  defaults: Partial<Record<AdminChannel, ManagedChannelSettings>>;
}

export interface AdminUser {
  userId: string;
  workspaceId: string;
  displayName: string;
  role: 'admin' | 'member';
  disabled: boolean;
  createdAt: number;
}

export interface AdminUserPage {
  users: AdminUser[];
  nextOffset: number | null;
}

export interface AdminInvite {
  id: string;
  userId: string;
  createdAt: number;
  expiresAt: number;
  redeemedAt: number | null;
  revokedAt: number | null;
}

export interface AdminInvitePage {
  invites: AdminInvite[];
  nextOffset: number | null;
}

export interface IssuedInvite {
  id: string;
  token: string;
  expiresAt: number;
}

export interface RouteForm {
  routeKind: RouteKind;
  provider: string;
  model: string;
  baseUrl: string;
  protocol: string;
  secret: string;
  secretFields: Record<string, string>;
  options: string;
  allowedModels: string;
}

export type ControlCall = <T>(path: string, init?: RequestInit) => Promise<T>;

export function emptyRouteForm(): RouteForm {
  return {
    routeKind: 'complete',
    provider: '',
    model: '',
    baseUrl: '',
    protocol: '',
    secret: '',
    secretFields: {},
    options: '',
    allowedModels: '',
  };
}

function invalid(): never {
  throw new Error('settings_invalid');
}

function objectOf(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}

function boolOf(value: unknown): boolean {
  if (typeof value !== 'boolean') invalid();
  return value;
}

function stringOf(value: unknown): string {
  if (typeof value !== 'string' || !value) invalid();
  return value;
}

function nullableString(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== 'string') invalid();
  return value;
}

function intOf(value: unknown, minimum = 0): number {
  if (!Number.isInteger(value) || (value as number) < minimum) invalid();
  return value as number;
}

function nullableInt(value: unknown): number | null {
  if (value === null) return null;
  return intOf(value, 0);
}

function nextOffsetOf(value: unknown): number | null {
  if (value === null) return null;
  return intOf(value, 0);
}

function optionsOf(value: unknown): Record<string, unknown> {
  const raw = objectOf(value);
  return { ...raw };
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) invalid();
  return value.map((item) => {
    if (typeof item !== 'string' || !item) invalid();
    return item;
  });
}

function parseProviderSchema(value: unknown): ProviderSchema {
  const row = objectOf(value);
  if (!Array.isArray(row.keys)) invalid();
  const keys: ProviderKeySchema[] = row.keys.map((item) => {
    const key = objectOf(item);
    return {
      env: stringOf(key.env),
      label: stringOf(key.label),
      required: boolOf(key.required),
      secret: boolOf(key.secret),
      choices: key.choices === undefined ? [] : stringList(key.choices),
    };
  });
  return {
    name: stringOf(row.name),
    keys,
    override_allowed: boolOf(row.override_allowed),
    broker_adapter: nullableString(row.broker_adapter),
  };
}

function parseProviders(value: unknown): ProviderCatalog {
  if (value === undefined) return {};
  const row = objectOf(value);
  const result: ProviderCatalog = {};
  for (const channel of ADMIN_CHANNELS) {
    if (!(channel in row)) continue;
    const providers = objectOf(row[channel]);
    const parsed: Record<string, ProviderSchema> = {};
    for (const [providerId, schema] of Object.entries(providers)) {
      if (!providerId) invalid();
      parsed[providerId] = parseProviderSchema(schema);
    }
    result[channel] = parsed;
  }
  return result;
}

function sourceOf(value: unknown): ChannelSource {
  if (value !== 'none' && value !== 'admin' && value !== 'user') invalid();
  return value;
}

function modeOf(value: unknown): ChannelMode {
  if (value !== 'none' && value !== 'complete'
      && value !== 'managed-model' && value !== 'inherited') invalid();
  return value;
}

export function parseChannelSettings(value: unknown): ChannelSettings {
  const row = objectOf(value);
  const allowed = row.allowed_models === undefined
    || row.allowed_models === null
    ? {} : { allowed_models: stringList(row.allowed_models) };
  const personalModel = row.personal_model === undefined
    ? {} : { personal_model: nullableString(row.personal_model) };
  const personalOptions = row.personal_options === undefined
    ? {} : { personal_options: optionsOf(row.personal_options) };
  return {
    configured: boolOf(row.configured),
    source: sourceOf(row.source),
    version: intOf(row.version),
    mode: modeOf(row.mode),
    provider: nullableString(row.provider),
    model: nullableString(row.model),
    base_url: nullableString(row.base_url),
    protocol: nullableString(row.protocol),
    options: optionsOf(row.options),
    ...allowed,
    ...personalModel,
    ...personalOptions,
    broker_adapter: nullableString(row.broker_adapter),
    broker_supported: boolOf(row.broker_supported),
    override_allowed: boolOf(row.override_allowed),
  };
}

export function parseModelSettings(value: unknown): ModelSettings {
  const row = objectOf(value);
  const channels = objectOf(row.channels);
  const parsed = {} as Record<AdminChannel, ChannelSettings>;
  for (const channel of ADMIN_CHANNELS) {
    if (!(channel in channels)) invalid();
    parsed[channel] = parseChannelSettings(channels[channel]);
  }
  return {
    version: intOf(row.version),
    channels: parsed,
    providers: parseProviders(row.providers),
  };
}

export function parseHarnessSummary(value: unknown): HarnessSummary {
  const row = objectOf(value);
  return {
    locked: boolOf(row.locked),
    configured: boolOf(row.configured),
    runtime: nullableString(row.runtime),
    revision: nullableInt(row.revision),
    model: nullableString(row.model),
    sandbox: nullableString(row.sandbox),
    timeout: nullableInt(row.timeout),
    codexAutoApprove: row.codexAutoApprove === null
      ? null : boolOf(row.codexAutoApprove),
    unavailableReason: nullableString(row.unavailableReason),
  };
}

export function parseManagedSettings(value: unknown): ManagedSettings {
  const row = objectOf(value);
  const defaults = objectOf(row.defaults);
  const parsed: Partial<Record<AdminChannel, ManagedChannelSettings>> = {};
  for (const channel of ADMIN_CHANNELS) {
    if (!(channel in defaults)) continue;
    const entry = objectOf(defaults[channel]);
    const allowed = entry.allowed_models === undefined
      || entry.allowed_models === null
      ? {} : { allowed_models: stringList(entry.allowed_models) };
    parsed[channel] = {
      provider: stringOf(entry.provider),
      model: stringOf(entry.model),
      base_url: stringOf(entry.base_url),
      protocol: stringOf(entry.protocol),
      options: optionsOf(entry.options),
      ...allowed,
      credential_configured: boolOf(entry.credential_configured),
    };
  }
  return { version: intOf(row.version), defaults: parsed };
}

export function parseAdminUser(value: unknown): AdminUser {
  const row = objectOf(value);
  const role = row.role;
  if (role !== 'admin' && role !== 'member') invalid();
  return {
    userId: stringOf(row.userId),
    workspaceId: stringOf(row.workspaceId),
    displayName: stringOf(row.displayName),
    role,
    disabled: boolOf(row.disabled),
    createdAt: intOf(row.createdAt),
  };
}

export function parseAdminUserPage(value: unknown): AdminUserPage {
  const row = objectOf(value);
  if (!Array.isArray(row.users)) invalid();
  return {
    users: row.users.map(parseAdminUser),
    nextOffset: nextOffsetOf(row.nextOffset),
  };
}

export function parseAdminInvite(value: unknown): AdminInvite {
  const row = objectOf(value);
  return {
    id: stringOf(row.id),
    userId: stringOf(row.userId),
    createdAt: intOf(row.createdAt),
    expiresAt: intOf(row.expiresAt),
    redeemedAt: nullableInt(row.redeemedAt),
    revokedAt: nullableInt(row.revokedAt),
  };
}

export function parseAdminInvitePage(value: unknown): AdminInvitePage {
  const row = objectOf(value);
  if (!Array.isArray(row.invites)) invalid();
  return {
    invites: row.invites.map(parseAdminInvite),
    nextOffset: nextOffsetOf(row.nextOffset),
  };
}

export function parseIssuedInvite(value: unknown): IssuedInvite {
  const row = objectOf(value);
  return {
    id: stringOf(row.id),
    token: stringOf(row.token),
    expiresAt: intOf(row.expiresAt),
  };
}

function hasControlCharacters(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    if (value.charCodeAt(index) < 32) return true;
  }
  return false;
}

export function parseOptionsText(text: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    throw new Error('options_invalid');
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('options_invalid');
  }
  const result: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(parsed as Record<string, unknown>)) {
    if (!key || key !== key.trim()) throw new Error('options_invalid');
    if (typeof item === 'number' && !Number.isFinite(item)) {
      throw new Error('options_invalid');
    }
    const type = typeof item;
    if (item !== null && type !== 'string' && type !== 'number'
        && type !== 'boolean') {
      // 嵌套对象/数组可能藏入 Authorization/headers 等凭证结构
      throw new Error('options_invalid');
    }
    if (type === 'string' && hasControlCharacters(item as string)) {
      throw new Error('options_invalid');
    }
    result[key] = item;
  }
  return result;
}

function completeRoute(form: RouteForm,
                       includeAllowedModels = false): Record<string, unknown> {
  const route: Record<string, unknown> = {
    route_kind: 'complete',
    provider: form.provider.trim(),
    model: form.model.trim(),
    base_url: form.baseUrl.trim(),
    protocol: form.protocol.trim(),
    options: parseOptionsText(form.options),
  };
  if (includeAllowedModels) {
    const allowed = (form.allowedModels || '').split(',')
      .map((item) => item.trim()).filter(Boolean);
    const unique = Array.from(new Set(allowed));
    if (unique.length > 64) throw new Error('options_invalid');
    if (unique.length) route.allowed_models = unique;
  }
  if (form.secret) route.secret = form.secret;
  return route;
}

export function isBaseUrlEnv(env: string): boolean {
  return env.endsWith('BASE_URL') || env.endsWith('_BASE');
}

/** 需要用户输入的凭证字段：必需秘密字段 + 必需非秘密字段（排除地址）。 */
export function credentialFields(
  schema?: ProviderSchema | null,
): ProviderKeySchema[] {
  if (!schema) return [];
  return schema.keys.filter((key) => key.required
    && (key.secret || !isBaseUrlEnv(key.env)));
}

/**
 * 单 Key 继续提交字符串；多字段/含必填非秘密字段时提交对象。
 * 全部留空表示复用本人同作用域旧凭证；部分填写明确拒绝。
 */
export function buildCredentialSecret(
  form: RouteForm,
  schema?: ProviderSchema | null,
): string | Record<string, string> | undefined {
  const fields = credentialFields(schema);
  if (!fields.length) return form.secret || undefined;
  if (fields.length === 1 && fields[0].secret) {
    return form.secret || undefined;
  }
  const values: Array<[string, string]> = [];
  let filled = 0;
  for (const field of fields) {
    const raw = form.secretFields[field.env] || '';
    if (!raw.trim()) continue;
    filled += 1;
    values.push([field.env, field.secret ? raw : raw.trim()]);
  }
  if (filled === 0) return undefined;
  if (filled !== fields.length) throw new Error('credentials_incomplete');
  return Object.fromEntries(values);
}

export function buildPersonalRoute(
  form: RouteForm,
  schema?: ProviderSchema | null,
): Record<string, unknown> {
  if (form.routeKind === 'managed-model') {
    const route: Record<string, unknown> = {
      route_kind: 'managed-model',
      options: parseOptionsText(form.options),
    };
    if (form.model.trim()) route.model = form.model.trim();
    return route;
  }
  const route = completeRoute(form);
  // 个人凭证由 schema 决定的字符串/对象统一生成，避免残留旧的单 Key 值
  delete route.secret;
  const secret = buildCredentialSecret(form, schema);
  if (secret !== undefined) route.secret = secret;
  return route;
}

export function buildManagedDefault(form: RouteForm): Record<string, unknown> {
  return completeRoute({ ...form, routeKind: 'complete' }, true);
}

export function prettyOptions(options: Record<string, unknown>): string {
  return Object.keys(options).length
    ? JSON.stringify(options, null, 2) : '';
}

export function formFromChannel(settings: ModelSettings,
                                channel: SettingsChannel): RouteForm {
  const entry = settings.channels[channel];
  if (!entry) return emptyRouteForm();
  const managed = entry.source === 'user' && entry.mode === 'managed-model';
  return {
    routeKind: managed ? 'managed-model' : 'complete',
    provider: entry.provider || '',
    // managed-model 只编辑个人显式值：省略 model 时保持空并继续跟随默认
    model: managed
      ? (entry.personal_model ?? '')
      : (entry.model || ''),
    baseUrl: entry.base_url || '',
    protocol: entry.protocol || '',
    secret: '',
    secretFields: {},
    options: managed
      ? prettyOptions(entry.personal_options || {})
      : prettyOptions(entry.options),
    allowedModels: '',
  };
}

export function formFromManaged(managed: ManagedSettings,
                                channel: AdminChannel): RouteForm {
  const entry = managed.defaults[channel];
  if (!entry) return emptyRouteForm();
  return {
    routeKind: 'complete',
    provider: entry.provider,
    model: entry.model,
    baseUrl: entry.base_url,
    protocol: entry.protocol,
    secret: '',
    secretFields: {},
    options: prettyOptions(entry.options),
    allowedModels: (entry.allowed_models || []).join(', '),
  };
}

export function channelLabel(channel: AdminChannel): string {
  switch (channel) {
    case 'speech': return '语音';
    case 'image': return '图像';
    case 'video': return '视频';
    case 'music': return '音乐';
    case 'chat': return '主聊天';
    default: return channel;
  }
}

export function sourceLabel(entry: ChannelSettings): string {
  if (entry.source === 'user') {
    return `个人配置（${entry.mode} · v${entry.version}）`;
  }
  if (entry.source === 'admin') {
    return `管理员默认（${entry.mode} · v${entry.version}）`;
  }
  return '未配置';
}

export function isPendingInvite(invite: AdminInvite,
                                now = Math.floor(Date.now() / 1000)): boolean {
  return invite.revokedAt === null
    && (invite.expiresAt === 0 || invite.expiresAt > now);
}

export function inviteStatusLabel(invite: AdminInvite,
                                  now = Math.floor(Date.now() / 1000)):
  '永久有效' | '已撤销' | '已过期' | '有效' {
  if (invite.revokedAt !== null) return '已撤销';
  if (invite.expiresAt === 0) return '永久有效';
  if (invite.expiresAt <= now) return '已过期';
  return '有效';
}

export function errorStatus(error: unknown): number | undefined {
  if (error && typeof error === 'object' && 'status' in error) {
    const status = (error as { status?: unknown }).status;
    if (typeof status === 'number') return status;
  }
  return undefined;
}

export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
