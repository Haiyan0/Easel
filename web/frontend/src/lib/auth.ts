/**
 * control 身份与请求生命周期。
 *
 * 边界：
 * - control/local 由服务端 /api/runtime 决定；未知值失败关闭。
 * - Cookie 由服务端 HttpOnly 设置；csrf 只保存在当前身份内存。
 * - 每次认证成功（含同 user/workspace 重新登录）都建立新 epoch；
 *   fetch/SSE/回调在提交 UI、缓存或 storage 前必须复核 epoch。
 * - 401 只使捕获它的所属 epoch 失效；403/503 保留明确错误，不回落 local。
 */

export type AuthRole = 'admin' | 'member';

export interface AuthContext {
  userId: string;
  workspaceId: string;
  role: AuthRole;
  csrfToken: string;
  capabilities: string[];
}

export class AuthError extends Error {
  status: number;
  code: string;
  /** 401 是否由当前身份 epoch 的请求触发；旧 epoch 迟到 401 为 false。 */
  identityInvalidated?: boolean;

  constructor(status: number, code: string, message?: string) {
    super(message || code);
    this.name = 'AuthError';
    this.status = status;
    this.code = code;
  }
}

function asIdentity(value: unknown): AuthContext {
  if (!value || typeof value !== 'object') {
    throw new AuthError(502, 'invalid_identity');
  }
  const data = value as Record<string, unknown>;
  const userId = data.userId;
  const workspaceId = data.workspaceId;
  const role = data.role;
  const csrfToken = data.csrfToken;
  const capabilities = data.capabilities;
  if (typeof userId !== 'string' || !userId
      || typeof workspaceId !== 'string' || !workspaceId
      || (role !== 'admin' && role !== 'member')
      || typeof csrfToken !== 'string' || !csrfToken
      || !Array.isArray(capabilities)
      || capabilities.some((item) => typeof item !== 'string')) {
    throw new AuthError(502, 'invalid_identity');
  }
  return {
    userId,
    workspaceId,
    role,
    csrfToken,
    capabilities: capabilities.slice(),
  };
}

async function errorFrom(response: Response): Promise<AuthError> {
  let code = '';
  try {
    const payload = await response.json() as Record<string, unknown>;
    const candidate = payload?.code ?? payload?.detail;
    if (typeof candidate === 'string' && candidate) code = candidate;
  } catch {
    /* non-JSON error body */
  }
  return new AuthError(response.status, code || `http_${response.status}`);
}

/** 读取当前登录；401 表示匿名，其它错误必须显示为服务故障而非 local。 */
export async function loadIdentity(
  fetchImpl: typeof fetch = fetch,
): Promise<AuthContext> {
  let response: Response;
  try {
    response = await fetchImpl('/api/auth/me', {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    throw new AuthError(0, 'network_unavailable');
  }
  if (response.status === 401) {
    throw new AuthError(401, 'unauthorized');
  }
  if (!response.ok) throw await errorFrom(response);
  return asIdentity(await response.json());
}

/** 登录页错误文案：保留状态语义，不把 503 说成“没有邀请”。 */
export function loginErrorMessage(error: unknown): string {
  if (error instanceof AuthError) {
    if (error.status === 401) {
      return '邀请无效、已使用或已过期，请联系管理员重新获取。';
    }
    if (error.status === 403) {
      return '当前来源或账号权限不允许登录。请从管理员提供的地址重新打开，或联系管理员确认账号状态。';
    }
    if (error.status === 429) return '尝试次数过多，请稍后再试。';
    if (error.status === 0) return '网络不可用，请检查连接后重试。';
    if (error.status >= 500) return '服务暂不可用，请稍后重试。';
    return error.code || '邀请兑换失败。';
  }
  return '邀请兑换失败，请稍后重试。';
}

/** 邀请兑换；明文只存在于本次调用，不写入任何存储。 */
export async function exchangeInvite(
  token: string,
  fetchImpl: typeof fetch = fetch,
): Promise<AuthContext> {
  if (typeof token !== 'string' || !token) {
    throw new AuthError(400, 'invalid_invite');
  }
  let response: Response;
  try {
    response = await fetchImpl('/api/auth/exchange', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
  } catch {
    throw new AuthError(0, 'network_unavailable');
  }
  if (!response.ok) throw await errorFrom(response);
  return asIdentity(await response.json());
}

/**
 * 服务端撤销当前 Cookie。调用方必须先用失效前的 csrf 发起本请求，
 * 且不能因为清理 UI 而中止它；失败要明确报告。
 */
export async function logout(
  ctx: AuthContext,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  let response: Response;
  try {
    response = await fetchImpl('/api/auth/logout', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-CSRF-Token': ctx.csrfToken },
    });
  } catch {
    throw new AuthError(0, 'network_unavailable');
  }
  if (!response.ok && response.status !== 401) {
    throw await errorFrom(response);
  }
}

/** 身份作用域缓存键；不同 user/workspace 永不共享内容键。 */
export function identityCacheKey(
  ctx: Pick<AuthContext, 'userId' | 'workspaceId'>,
  name: string,
): string {
  return [
    'easel',
    'control',
    'v1',
    encodeURIComponent(ctx.userId),
    encodeURIComponent(ctx.workspaceId),
    name,
  ].join(':');
}

export interface StorageLike {
  readonly length: number;
  key(index: number): string | null;
  removeItem(key: string): void;
}

/** 只清理该身份的私有键；共享主题等非身份键保持不变。 */
export function clearIdentityCache(
  ctx: Pick<AuthContext, 'userId' | 'workspaceId'>,
  storages: StorageLike[],
): void {
  const prefix = identityCacheKey(ctx, '');
  for (const storage of storages) {
    const keys: string[] = [];
    try {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key && key.startsWith(prefix)) keys.push(key);
      }
    } catch { /* storage disabled */ }
    for (const key of keys) {
      try { storage.removeItem(key); } catch { /* ignore */ }
    }
  }
}

/**
 * 身份 epoch：每次认证/重新登录都递增，invalidate 后旧 epoch 全部失效。
 * 同一 user/workspace 也不能复用旧号码。
 */
export class IdentityEpoch {
  private current = 0;
  private userId = '';
  private workspaceId = '';

  begin(ctx: Pick<AuthContext, 'userId' | 'workspaceId'>): number {
    this.current += 1;
    this.userId = ctx.userId;
    this.workspaceId = ctx.workspaceId;
    return this.current;
  }

  invalidate(): void {
    this.current += 1;
    this.userId = '';
    this.workspaceId = '';
  }

  isCurrent(epoch: number): boolean {
    return Number.isInteger(epoch) && epoch === this.current
      && !!this.userId && !!this.workspaceId;
  }

  snapshot(): { epoch: number; userId: string; workspaceId: string } {
    return {
      epoch: this.current,
      userId: this.userId,
      workspaceId: this.workspaceId,
    };
  }
}

/** 异步结果提交前复核 epoch；返回是否真正提交。 */
export function commitIfCurrent(
  epochs: IdentityEpoch,
  epoch: number,
  commit: () => void,
): boolean {
  if (!epochs.isCurrent(epoch)) return false;
  commit();
  return true;
}

export interface IdentityRequest {
  epochs: IdentityEpoch;
  epoch: number;
  ctx: AuthContext;
  url: string;
  init?: RequestInit;
  fetchImpl?: typeof fetch;
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

export interface IdentityResponse<T> {
  status: number;
  body: T;
}

/**
 * 统一同源请求层：写请求带内存 CSRF；401 只失效捕获的 epoch。
 * 调用方拿到结果后仍需用 commitIfCurrent 做最终提交校验。
 * raw 变体保留 HTTP 状态（如 202 接纳），但安全语义与 identityFetch 相同。
 */
export async function identityFetchRaw<T>(
  request: IdentityRequest,
): Promise<IdentityResponse<T>> {
  const fetchImpl = request.fetchImpl || fetch;
  const method = (request.init?.method || 'GET').toUpperCase();
  const headers: Record<string, string> = {
    ...((request.init?.headers as Record<string, string> | undefined) || {}),
  };
  if (!SAFE_METHODS.has(method)) {
    headers['X-CSRF-Token'] = request.ctx.csrfToken;
  }
  let response: Response;
  try {
    response = await fetchImpl(request.url, {
      ...request.init,
      method,
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    throw new AuthError(0, 'network_unavailable');
  }
  if (response.status === 401) {
    // 先判断是否属于当前身份，再失效并标记；调用方据此卸载私人树。
    // 旧 epoch 迟到的 401 不失效、不通知，不能把刚登录的 B 踢下线。
    const owned = request.epochs.isCurrent(request.epoch);
    if (owned) request.epochs.invalidate();
    const error = new AuthError(401, 'unauthorized');
    error.identityInvalidated = owned;
    throw error;
  }
  if (!response.ok) throw await errorFrom(response);
  if (response.status === 204) {
    return { status: 204, body: undefined as T };
  }
  return {
    status: response.status,
    body: await response.json() as T,
  };
}

export async function identityFetch<T>(
  request: IdentityRequest,
): Promise<T> {
  return (await identityFetchRaw<T>(request)).body;
}
