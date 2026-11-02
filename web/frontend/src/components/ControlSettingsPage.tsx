import { useEffect, useRef, useState } from 'react';
import type { AuthContext } from '../lib/auth';
import {
  PERSONAL_CHANNELS, buildPersonalRoute, channelLabel, credentialFields,
  emptyRouteForm, errorText, formFromChannel, parseHarnessSummary,
  parseModelSettings, sourceLabel,
} from '../lib/controlSettings';
import type {
  ControlCall, HarnessSummary, ModelSettings, ProviderSchema, RouteForm,
  SettingsChannel,
} from '../lib/controlSettings';

const panelStyle = {
  display: 'grid',
  gap: '16px',
  alignContent: 'start',
  maxWidth: '980px',
} as const;

const cardStyle = {
  border: '1px solid var(--border)',
  background: 'var(--surface)',
  borderRadius: 'var(--radius-lg)',
  padding: '16px',
  display: 'grid',
  gap: '10px',
} as const;

const buttonStyle = {
  border: '1px solid var(--border)',
  background: 'var(--bg-elev)',
  color: 'var(--text)',
  borderRadius: 'var(--radius)',
  padding: '8px 10px',
  cursor: 'pointer',
  fontSize: '13px',
} as const;

const inputStyle = {
  background: 'var(--bg-elev)',
  color: 'var(--text)',
  border: '1px solid var(--border)',
  borderRadius: '8px',
  padding: '8px 10px',
  fontSize: '13px',
  width: '100%',
  boxSizing: 'border-box',
} as const;

interface Props {
  ctx: AuthContext;
  call: ControlCall;
}

export default function ControlSettingsPage({ ctx, call }: Props) {
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [harness, setHarness] = useState<HarnessSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [channel, setChannel] = useState<SettingsChannel>('speech');
  const [form, setForm] = useState<RouteForm>(emptyRouteForm);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const loadSeq = useRef(0);
  const saveSeq = useRef(0);
  const formSeq = useRef(0);
  const mounted = useRef(true);
  const ownerRef = useRef(`${ctx.userId}:${ctx.workspaceId}`);
  const channelRef = useRef<SettingsChannel>('speech');
  ownerRef.current = `${ctx.userId}:${ctx.workspaceId}`;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      loadSeq.current += 1;
      saveSeq.current += 1;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    const seq = ++loadSeq.current;
    const owner = `${ctx.userId}:${ctx.workspaceId}`;
    setLoading(true);
    setLoadError('');
    Promise.all([
      call<unknown>('/api/settings/models'),
      call<unknown>('/api/settings/harness'),
    ]).then(([modelsRaw, harnessRaw]) => {
      if (!alive || !mounted.current || seq !== loadSeq.current
          || ownerRef.current !== owner) return;
      const parsed = parseModelSettings(modelsRaw);
      setSettings(parsed);
      setHarness(parseHarnessSummary(harnessRaw));
      setForm(formFromChannel(parsed, channelRef.current));
    }).catch((error: unknown) => {
      if (!alive || !mounted.current || seq !== loadSeq.current
          || ownerRef.current !== owner) return;
      setLoadError(`读取失败：${errorText(error)}`);
    }).finally(() => {
      if (alive && mounted.current && seq === loadSeq.current) {
        setLoading(false);
      }
    });
    return () => { alive = false; };
  }, [call, ctx.userId, ctx.workspaceId, reload]);

  const chooseChannel = (next: SettingsChannel) => {
    formSeq.current += 1;
    channelRef.current = next;
    setChannel(next);
    setNotice('');
    if (settings) setForm(formFromChannel(settings, next));
  };

  const updateForm = (patch: Partial<RouteForm>) => {
    formSeq.current += 1;
    setForm((current) => ({ ...current, ...patch }));
  };

  const changeProvider = (value: string) => {
    // 切换供应商时清空本次输入的凭证，避免环境变量名相同造成串用
    formSeq.current += 1;
    setForm((current) => ({
      ...current, provider: value, secret: '', secretFields: {},
    }));
  };

  const save = async () => {
    if (!settings) return;
    const seq = ++saveSeq.current;
    const owner = ownerRef.current;
    const generation = formSeq.current;
    const savedChannel = channel;
    setBusy(true);
    setNotice('');
    try {
      const route = buildPersonalRoute(form, providerSchema);
      await call('/api/settings/models/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, route }),
      });
      if (!mounted.current || seq !== saveSeq.current
          || ownerRef.current !== owner) return;
      if (generation !== formSeq.current
          || channelRef.current !== savedChannel) {
        // 响应属于上一份表单；不得清空或刷新用户正在编辑的当前表单
        setNotice('已保存上一份配置；当前表单未被覆盖。');
        return;
      }
      setForm((current) => ({
        ...current, secret: '', secretFields: {},
      }));
      setNotice('已保存个人配置。');
      setReload((value) => value + 1);
    } catch (error: unknown) {
      if (!mounted.current || seq !== saveSeq.current
          || ownerRef.current !== owner) return;
      const message = error instanceof Error
        && error.message === 'credentials_incomplete'
        ? '请填写全部必需凭证后再保存。'
        : errorText(error);
      setNotice(`保存失败：${message}`);
    } finally {
      if (mounted.current && seq === saveSeq.current) setBusy(false);
    }
  };

  const restore = async () => {
    const seq = ++saveSeq.current;
    const owner = ownerRef.current;
    const generation = formSeq.current;
    const restoredChannel = channel;
    setBusy(true);
    setNotice('');
    try {
      await call(`/api/settings/models/overrides/${channel}`, {
        method: 'DELETE',
      });
      if (!mounted.current || seq !== saveSeq.current
          || ownerRef.current !== owner) return;
      if (generation !== formSeq.current
          || channelRef.current !== restoredChannel) {
        setNotice('已恢复上一通道的继承设置；当前表单未刷新。');
        return;
      }
      setForm((current) => ({
        ...current, secret: '', secretFields: {},
      }));
      setNotice('已恢复继承管理员默认。');
      setReload((value) => value + 1);
    } catch (error: unknown) {
      if (!mounted.current || seq !== saveSeq.current
          || ownerRef.current !== owner) return;
      setNotice(`恢复失败：${errorText(error)}`);
    } finally {
      if (mounted.current && seq === saveSeq.current) setBusy(false);
    }
  };

  const entry = settings?.channels[channel];
  const managedMode = form.routeKind === 'managed-model';
  const providerSchema: ProviderSchema | undefined =
    settings?.providers?.[channel]?.[form.provider.trim()];
  const credentialInputs = credentialFields(providerSchema);
  const multifieldCredentials = credentialInputs.length > 0
    && !(credentialInputs.length === 1 && credentialInputs[0].secret);

  return (
    <section style={panelStyle} aria-label="个人设置">
      <header style={{ display: 'grid', gap: '6px' }}>
        <h1 style={{ margin: 0, fontSize: '20px' }}>个人设置</h1>
        <p style={{ margin: 0, color: 'var(--text-secondary)', fontSize: '13px' }}>
          只管理本人的能力模型覆盖；主聊天模型与受管底座由管理员统一配置。
        </p>
      </header>

      <section className="settings-section" style={cardStyle}
        aria-label="受管底座摘要">
        <h2 style={{ margin: 0, fontSize: '16px' }}>受管底座（只读）</h2>
        {harness ? (
          <>
            <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
              锁定：{harness.locked ? '是' : '否'} · 状态：
              {harness.configured ? '已配置' : '未配置'}
            </div>
            {harness.configured ? (
              <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                {harness.runtime ? `运行时：${harness.runtime}` : null}
                {harness.revision !== null
                  ? ` · 修订：${harness.revision}` : null}
                {harness.model ? ` · 模型：${harness.model}` : null}
                {harness.sandbox ? ` · 沙箱：${harness.sandbox}` : null}
              </div>
            ) : (
              <div style={{ fontSize: '13px', color: 'var(--amber)' }}>
                {harness.unavailableReason || '受管底座尚未装配。'}
              </div>
            )}
            <p style={{ margin: 0, color: 'var(--text-tertiary)', fontSize: '12px' }}>
              安装、命令、环境、沙箱与重启由管理员负责；本页没有写入口。
            </p>
          </>
        ) : (
          <p style={{ margin: 0, color: 'var(--text-secondary)', fontSize: '13px' }}>
            正在读取受管摘要…
          </p>
        )}
      </section>

      {loadError ? (
        <div role="alert" style={{ color: 'var(--red)', fontSize: '14px' }}>
          {loadError}
        </div>
      ) : null}
      {loading ? (
        <p style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>读取个人设置…</p>
      ) : null}

      {settings ? (
        <section className="settings-section" style={cardStyle}
          aria-label="个人能力模型">
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            {PERSONAL_CHANNELS.map((item) => (
              <button key={item} className="settings-choice" style={{
                ...buttonStyle,
                borderColor: item === channel
                  ? 'var(--accent-end)' : 'var(--border)',
                background: item === channel
                  ? 'var(--accent-soft)' : 'var(--bg-elev)',
                color: item === channel
                  ? 'var(--accent-end)' : 'var(--text)',
              }} aria-pressed={item === channel}
              disabled={busy || loading}
              onClick={() => chooseChannel(item)}>
                {channelLabel(item)}
              </button>
            ))}
          </div>

          {entry ? (
            <div style={{ display: 'grid', gap: '6px' }}>
              <div style={{ fontSize: '14px', color: 'var(--text)' }}>
                {channelLabel(channel)} · {sourceLabel(entry)}
              </div>
              <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                {entry.configured
                  ? `生效来源：${entry.source === 'user' ? '个人配置' : '管理员默认'} · ` +
                    `版本 ${entry.version}`
                  : '尚未配置；保存个人完整配置或等待管理员默认。'}
                {entry.provider ? ` · ${entry.provider}/${entry.model || ''}` : ''}
                {entry.broker_supported
                  ? ' · 转发适配已声明'
                  : ' · 当前无受管转发'}
              </div>
              <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
                受管转发只表示适配支持；实际连接与凭证以保存后的配置为准。
              </div>
            </div>
          ) : null}

          <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
            个人配置方式
            <select aria-label="个人配置方式" style={inputStyle}
              disabled={busy || loading}
              value={form.routeKind}
              onChange={(event) => {
                const next = event.target.value as RouteForm['routeKind'];
                updateForm(next === 'managed-model'
                  ? {
                    routeKind: next, model: '', options: '',
                    secret: '', secretFields: {},
                  }
                  : { routeKind: next, secret: '', secretFields: {} });
              }}>
              <option value="complete">个人完整路由</option>
              <option value="managed-model">使用管理员受管模型</option>
            </select>
          </label>

          {managedMode ? (
            <>
              {entry?.allowed_models?.length ? (
                <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
                  受管模型
                  <select aria-label="受管模型" style={inputStyle}
              disabled={busy || loading}
                    value={form.model}
                    onChange={(event) => updateForm({
                      model: event.target.value }) }>
                    <option value="">使用管理员默认模型</option>
                    {entry.allowed_models.map((model) => (
                      <option key={model} value={model}>{model}</option>
                    ))}
                    {form.model
                      && !entry.allowed_models.includes(form.model) ? (
                        <option value={form.model}>
                          {form.model}（当前固定）
                        </option>
                      ) : null}
                  </select>
                </label>
              ) : (
                <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
                  模型（可选）
                  <input aria-label="模型" style={inputStyle}
                    disabled={busy || loading}
                    value={form.model}
                    onChange={(event) => updateForm({
                      model: event.target.value })} />
                </label>
              )}
              <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
                非敏感参数（JSON）
                <textarea aria-label="非敏感参数（JSON）" rows={3}
                  disabled={busy || loading}
                  style={inputStyle} value={form.options}
                  onChange={(event) => updateForm({
                    options: event.target.value })} />
              </label>
              <p style={{ margin: 0, color: 'var(--text-tertiary)', fontSize: '12px' }}>
                只选择管理员批准的模型与非敏感参数；地址与凭证仍来自当前受管版本。
              </p>
            </>
          ) : (
            <>
              <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
                供应商
                <input aria-label="供应商" style={inputStyle}
                  disabled={busy || loading}
                  value={form.provider}
                  onChange={(event) => changeProvider(
                    event.target.value)} />
              </label>
              <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
                模型
                <input aria-label="模型" style={inputStyle}
                  disabled={busy || loading}
                  value={form.model}
                  onChange={(event) => updateForm({
                    model: event.target.value })} />
              </label>
              <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
                API 地址
                <input aria-label="API 地址" style={inputStyle}
                  disabled={busy || loading}
                  value={form.baseUrl}
                  onChange={(event) => updateForm({
                    baseUrl: event.target.value })} />
              </label>
              <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
                协议
                <input aria-label="协议" style={inputStyle}
                  disabled={busy || loading}
                  value={form.protocol}
                  onChange={(event) => updateForm({
                    protocol: event.target.value })} />
              </label>
              {multifieldCredentials ? credentialInputs.map((field) => (
                <label key={field.env} style={{
                  display: 'grid', gap: '4px', fontSize: '13px',
                }}>
                  {`凭证 · ${field.label}（必填）`}
                  <input aria-label={`凭证 ${field.label}`}
                    type={field.secret ? 'password' : 'text'}
                    autoComplete="new-password" style={inputStyle}
                    disabled={busy || loading}
                    value={form.secretFields[field.env] || ''}
                    onChange={(event) => {
                      formSeq.current += 1;
                      const value = event.target.value;
                      setForm((current) => ({
                        ...current,
                        secretFields: {
                          ...current.secretFields,
                          [field.env]: value,
                        },
                      }));
                    }} />
                </label>
              )) : (
                <label style={{
                  display: 'grid', gap: '4px', fontSize: '13px',
                }}>
                  API Key
                  <input aria-label="API Key" type="password"
                    autoComplete="new-password" style={inputStyle}
                    disabled={busy || loading}
                    value={form.secret}
                    onChange={(event) => updateForm({
                      secret: event.target.value })} />
                </label>
              )}
              <label style={{ display: 'grid', gap: '4px', fontSize: '13px' }}>
                非敏感参数（JSON）
                <textarea aria-label="非敏感参数（JSON）" rows={3}
                  disabled={busy || loading}
                  style={inputStyle} value={form.options}
                  onChange={(event) => updateForm({
                    options: event.target.value })} />
              </label>
              <p style={{ margin: 0, color: 'var(--text-tertiary)', fontSize: '12px' }}>
                {multifieldCredentials
                  ? '全部留空只复用本人同供应商、地址与协议的已有凭证；'
                    + '更换地址/协议/供应商或轮换时必须填写全部必需字段。'
                  : '留空 Key 只复用本人同供应商、地址与协议的已有凭证；'
                    + '不会复制管理员 Key。更换地址/协议/供应商时必须重新输入。'}
              </p>
            </>
          )}

          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            <button style={{ ...buttonStyle, borderColor: 'var(--accent-end)' }}
              disabled={busy || loading}
              onClick={() => { void save(); }}>
              保存个人配置
            </button>
            {entry?.source === 'user' ? (
              <button style={buttonStyle} disabled={busy || loading}
                onClick={() => { void restore(); }}>
                恢复继承管理员默认
              </button>
            ) : null}
            <button style={buttonStyle} disabled={busy}
              onClick={() => setReload((value) => value + 1)}>
              重新读取
            </button>
          </div>
          {notice ? (
            <div role="status" style={{
              color: notice.includes('失败') ? 'var(--red)' : 'var(--green)',
              fontSize: '13px',
            }}>{notice}</div>
          ) : null}
        </section>
      ) : null}
    </section>
  );
}
