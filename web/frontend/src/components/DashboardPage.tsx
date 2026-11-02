import { useState, useEffect, useRef, useCallback } from 'react';
import type {
  TrendGroup, ScheduleItem, OutputNode, AccountItem, Idea,
  AnalyticsPlatform, AccountAnalytics, AccountWhoami,
} from '../lib/api';
import type { Page } from './Sidebar';
import { useWorkspaceData } from '../lib/workspaceDataContext';
import { isIdentityExpired, workspaceErrorMessage } from '../lib/workspaceData';
import { dashboardHeroText } from '../lib/statusCopy';
import {
  IconFire, IconCalendar, IconOutputs, IconChat, IconSkills, IconAccounts,
  IconIdea, IconPublish,
} from './icons';

/** 大数格式化：12000 → 1.2万。 */
function fmtNum(n: number | null): string {
  if (n == null) return '—';
  const a = Math.abs(n);
  if (a >= 10000) return (n / 10000).toFixed(a >= 100000 ? 0 : 1) + '万';
  return String(n);
}
/** 增长量渲染信息：正=绿↑，负=红↓，0/缺失=不显示。 */
function growthInfo(n: number | null): { text: string; color: string } | null {
  if (n == null || n === 0) return null;
  return n > 0
    ? { text: `▲+${fmtNum(n)}`, color: 'var(--trend-up)' }
    : { text: `▼${fmtNum(Math.abs(n))}`, color: 'var(--trend-down)' };
}

/** 真实取数时间；0/缺失明确显示未知，不用本次请求时间冒充。 */
function fetchedAtText(value: number): string {
  if (!value || !Number.isFinite(value)) return '取数时间未知';
  const ms = value > 1e12 ? value : value * 1000;
  try {
    return new Date(ms).toLocaleString();
  } catch {
    return '取数时间未知';
  }
}

interface DashboardProps {
  persona: string;
  gatewayStatus: string;
  apiDirect: boolean;
  agentRuntime: string;
  onNavigate: (page: Page) => void;
  onUseTopic: (title: string) => void;
}

const STATUS_LABEL: Record<string, string> = { idea: '选题', draft: '草稿', scheduled: '待发', published: '已发' };

export default function DashboardPage({ persona, gatewayStatus, apiDirect, agentRuntime, onNavigate, onUseTopic }: DashboardProps) {
  const data = useWorkspaceData();
  const accountsEnabled = data.accountsEnabled;
  const expireIfNeeded = useCallback((error: unknown) => {
    if (isIdentityExpired(error)) data.expireIdentity?.();
  }, [data]);
  const [trends, setTrends] = useState<TrendGroup[]>([]);
  const [schedule, setSchedule] = useState<ScheduleItem[]>([]);
  const [outputs, setOutputs] = useState<OutputNode[]>([]);
  const [accounts, setAccounts] = useState<AccountItem[]>([]);
  const [ideas, setIdeas] = useState<Idea[]>([]);
  const [trendStale, setTrendStale] = useState(false);
  const [loading, setLoading] = useState(true);
  const [workbenchError, setWorkbenchError] = useState('');
  const request = useRef<AbortController | null>(null);
  // 归因层：账号创作数据。control 的账号/分析属于 5C，不能读取 local 缓存。
  const [anaPlats, setAnaPlats] = useState<AnalyticsPlatform[]>([]);
  const [anaSel, setAnaSel] = useState('');
  const [anaData, setAnaData] = useState<Record<string, AccountAnalytics | 'loading' | 'error'>>(() => {
    if (!accountsEnabled) return {};
    const cache = data.getAnalyticsCache();
    return Object.fromEntries(Object.entries(cache).filter(([platform, value]) =>
      value && typeof value === 'object' && value.platform === platform
      && typeof value.loggedIn === 'boolean'));
  });
  const anaPending = useRef(new Set<string>());
  const [anaErrors, setAnaErrors] = useState<Record<string, string>>({});
  const [anaWin, setAnaWin] = useState<'last' | 'day' | 'week' | 'month' | 'year'>('week');
  // whoami 自愈：登录态以真实 profile 为准（与账号页共享同一适配器缓存）
  const whoamiMap: Record<string, AccountWhoami> =
    accountsEnabled ? data.whoami.get() : {};
  // 与 AccountsPage 同款：卸载后不再触发 setState（whoami 每个平台要起浏览器，最慢数秒）
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setLoading(true);
    setWorkbenchError('');
    setTrendStale(false);

    if (data.workbenchEnabled) {
      const note = (error: unknown, fallback: string) => {
        expireIfNeeded(error);
        if (!controller.signal.aborted) {
          setWorkbenchError(workspaceErrorMessage(error, fallback));
        }
      };
      const tasks = [
        data.fetchTrends('weibo,douyin', 6, controller.signal)
          .then((value) => {
            if (controller.signal.aborted) return;
            setTrends(value.trends);
            setTrendStale(value.stale === true
              || value.trends.some((group) => group.stale === true));
          })
          .catch((error) => note(error, '热点读取失败，请重试。')),
        data.fetchSchedule(controller.signal)
          .then((value) => { if (!controller.signal.aborted) setSchedule(value); })
          .catch((error) => note(error, '日历读取失败，请重试。')),
        data.fetchOutputs()
          .then((value) => { if (!controller.signal.aborted) setOutputs(value); })
          .catch((error) => note(error, '内容库读取失败，请重试。')),
        data.fetchIdeas(controller.signal)
          .then((value) => { if (!controller.signal.aborted) setIdeas(value); })
          .catch((error) => note(error, '选题读取失败，请重试。')),
      ];
      void Promise.allSettled(tasks).then(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    } else {
      setLoading(false);
    }

    if (!accountsEnabled) return () => {
      controller.abort();
    };

    data.fetchAccounts().then(setAccounts).catch(expireIfNeeded);
    data.fetchAnalyticsPlatforms().then((ps) => {
      setAnaPlats(ps);
      const cache = data.whoami.get();
      const isLog = (p: AnalyticsPlatform) => p.loggedIn || !!cache[p.platform]?.loggedIn;
      const first = ps.find(isLog);
      if (first) setAnaSel((s) => s || first.platform);
      // control 工作台不再为全部平台自动起浏览器；登录态以快照/账号页
      // 校验结果为准，需要强制校验时由用户在账号页显式触发。
    }).catch(expireIfNeeded);
    return () => {
      controller.abort();
    };
  }, [data, accountsEnabled, expireIfNeeded]);

  const runAna = (platform: string) => {
    setAnaSel(platform);
    if (anaPending.current.has(platform)) return;
    anaPending.current.add(platform);
    setAnaErrors((errors) => ({ ...errors, [platform]: '' }));
    setAnaData((d) => ({ ...d, [platform]: 'loading' }));
    data.fetchAccountAnalytics(platform)
      .then((r) => setAnaData((d) => {
        const next = { ...d, [platform]: r };
        const cache: Record<string, AccountAnalytics> = {};
        for (const [key, value] of Object.entries(next)) {
          if (value && typeof value === 'object') {
            cache[key] = value as AccountAnalytics;
          }
        }
        data.setAnalyticsCache(cache);
        return next;
      }))
      .catch((error: unknown) => {
        expireIfNeeded(error);
        const reason = workspaceErrorMessage(error, '抓取失败，请稍后重试');
        const cached = data.getAnalyticsCache()[platform];
        if (cached) {
          // 刷新失败时保留可读旧数据，但明确 stale 与真实取数时间；
          // 不伪造零值，也不把本次失败时间当 fetched_at。
          setAnaData((d) => ({
            ...d, [platform]: { ...cached, stale: true, warning: '' },
          }));
          setAnaErrors((errors) => ({
            ...errors, [platform]: `${reason}；显示旧数据`,
          }));
        } else {
          setAnaErrors((errors) => ({ ...errors, [platform]: reason }));
          setAnaData((d) => ({ ...d, [platform]: 'error' as const }));
        }
      })
      .finally(() => anaPending.current.delete(platform));
  };

  const hour = new Date().getHours();
  const greet = hour < 6 ? '夜深了' : hour < 12 ? '上午好' : hour < 14 ? '中午好' : hour < 18 ? '下午好' : '晚上好';
  // 本地日期（与日历页 ymd 一致）：toISOString 是 UTC，会把「今天」错判成昨天/明天
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  const upcoming = [...schedule]
    .filter((s) => s.date >= todayStr && s.status !== 'published')
    .sort((a, b) => (a.date + a.time).localeCompare(b.date + b.time)).slice(0, 5);
  const recent = outputs.slice(0, 5);
  const pendingIdeas = ideas.filter((i) => i.status === 'pending');
  const loggedIn = accounts.filter((a) => a.loggedIn).length;

  const quick: { label: string; page: Page; Icon: typeof IconChat }[] = [
    { label: '开始对话', page: 'chat', Icon: IconChat },
    { label: '看热点', page: 'trends', Icon: IconFire },
    { label: '拆爆款', page: 'breakdown', Icon: IconSkills },
    { label: '记选题', page: 'ideas', Icon: IconIdea },
    { label: '排日历', page: 'calendar', Icon: IconCalendar },
    { label: '去发布', page: 'publish', Icon: IconPublish },
  ];

  const stats: { label: string; value: string; page: Page; Icon: typeof IconChat }[] = [
    { label: '待做选题', value: String(pendingIdeas.length), page: 'ideas', Icon: IconIdea },
    { label: '待发排期', value: String(upcoming.length), page: 'calendar', Icon: IconCalendar },
    { label: '内容项目', value: String(outputs.length), page: 'outputs', Icon: IconOutputs },
    { label: '已登录账号', value: accountsEnabled ? `${loggedIn}/${accounts.length}` : '未接通', page: 'accounts', Icon: IconAccounts },
  ];

  return (
    <div className="page-scroll dash-page">
      <div className="dash-hero">
        <h1 className="page-title" style={{ fontSize: 26 }}>{greet} 👋</h1>
        <p className="page-subtitle">
          {dashboardHeroText(gatewayStatus, apiDirect, agentRuntime)}
          {persona ? ` 当前画像「${persona}」。` : ' 通用模式——指定画像效果更好。'}
          从热点到发布，一站式搞定今天的内容。
        </p>
        <div className="dash-quick">
          {quick.map((q) => (
            <button key={q.page} className="dash-quick-btn" onClick={() => onNavigate(q.page)}>
              <q.Icon size={16} /><span>{q.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* 概览数字 */}
      {workbenchError && <div className="notice-error" role="alert">{workbenchError}</div>}
      {loading && <div className="dash-empty">工作台数据加载中…</div>}

      <div className="dash-stats">
        {stats.map((s) => (
          <button key={s.label} className="card card-hover dash-stat" onClick={() => onNavigate(s.page)}>
            <span className="dash-stat-ic"><s.Icon size={18} /></span>
            <span className="dash-stat-val">{s.value}</span>
            <span className="dash-stat-label">{s.label}</span>
          </button>
        ))}
      </div>

      <div className="dash-grid">
        {/* 今日热点 */}
        <div className="card dash-card">
          <div className="dash-card-head">
            <span>
              <IconFire size={16} /> 今日热点
              {trendStale && (
                <span className="badge" style={{ marginLeft: 6 }}>旧数据</span>
              )}
            </span>
            <button className="dash-more" onClick={() => onNavigate('trends')}>热点雷达 →</button>
          </div>
          {trendStale && (
            <div className="dash-empty" role="status">
              热点刷新失败，显示缓存数据；时效以热点雷达页为准。
            </div>
          )}
          {trends.length === 0 && !trendStale && (
            <div className="dash-empty">
              {loading ? '热点加载中…' : workbenchError ? '热点读取失败' : '暂无热点'}
            </div>
          )}
          {trends.map((g) => (
            <div key={g.platform} className="dash-trend-group">
              <div className="dash-trend-plat">
                {g.label}{g.stale ? ' · 旧数据' : ''}
              </div>
              {g.items.slice(0, 3).map((it, i) => (
                <div key={i} className="dash-trend-item" title={`${it.title}（点击做成内容）`}>
                  <span className="dash-trend-title" onClick={() => onUseTopic(it.title)}>{it.title}</span>
                </div>
              ))}
            </div>
          ))}
        </div>

        {/* 选题库 */}
        <div className="card dash-card">
          <div className="dash-card-head">
            <span><IconIdea size={16} /> 选题库 · 待做</span>
            <button className="dash-more" onClick={() => onNavigate('ideas')}>全部 →</button>
          </div>
          {pendingIdeas.length === 0 && (
            <div className="dash-empty">
              {loading ? '选题加载中…' : workbenchError ? '选题读取失败' : '还没攒选题，去热点雷达收藏几个吧'}
            </div>
          )}
          {pendingIdeas.slice(0, 5).map((it) => (
            <div key={it.id} className="dash-idea" onClick={() => onUseTopic(it.title)} title="点击做成内容">
              <span className="dash-idea-title">{it.title}</span>
              {it.source && <span className="badge">{it.source}</span>}
            </div>
          ))}
        </div>

        {/* 近期排期 */}
        <div className="card dash-card">
          <div className="dash-card-head">
            <span><IconCalendar size={16} /> 近期排期</span>
            <button className="dash-more" onClick={() => onNavigate('calendar')}>日历 →</button>
          </div>
          {upcoming.length === 0 && (
            <div className="dash-empty">
              {loading ? '排期加载中…' : workbenchError ? '排期读取失败' : '暂无排期，去日历安排一条吧'}
            </div>
          )}
          {upcoming.map((s) => (
            <div key={s.id} className="dash-sched" onClick={() => onNavigate('calendar')}>
              <span className="dash-sched-date">{s.date.slice(5)}</span>
              <span className="dash-sched-title">{s.platform ? `[${s.platform}] ` : ''}{s.title}</span>
              <span className="badge">{STATUS_LABEL[s.status] || s.status}</span>
            </div>
          ))}
        </div>

        {/* 最近产物 */}
        <div className="card dash-card dash-card-top">
          <div className="dash-card-head">
            <span><IconOutputs size={16} /> 最近产物</span>
            <button className="dash-more" onClick={() => onNavigate('outputs')}>内容库 →</button>
          </div>
          {recent.length === 0 && (
            <div className="dash-empty">
              {loading ? '产物加载中…' : workbenchError ? '产物读取失败' : '还没有产物，去对话生成第一条吧'}
            </div>
          )}
          {recent.map((g) => (
            <div key={g.name} className="dash-output" onClick={() => onNavigate('outputs')}>
              <span className="dash-output-name">{g.meta?.title || g.name}</span>
              <span className="badge">{g.meta?.platform || (g.type === 'dir' ? `${g.fileCount ?? 0} 文件` : '单文件')}</span>
            </div>
          ))}
        </div>

        {/* 创作数据（归因层）：选平台自动拉取登录账号的粉丝/获赞/关注 + 多窗口增长 + 近7日环比 + 最新笔记 */}
        <div className="card dash-card dash-card-wide">
          <div className="dash-card-head">
            <span><IconAccounts size={16} /> 创作数据</span>
            {anaSel && anaData[anaSel] && anaData[anaSel] !== 'loading' && (
              <button className="dash-more" onClick={() => runAna(anaSel)}>刷新 →</button>
            )}
          </div>
          {(() => {
            if (!accountsEnabled) {
              return (
                <div className="dash-empty" role="status">
                  账号与创作数据将在 5C 接通；当前未读取本机账号、创作指标或分析缓存。
                </div>
              );
            }
            const logged = anaPlats.filter((p) => p.loggedIn || whoamiMap[p.platform]?.loggedIn);
            if (anaPlats.length === 0) return <div className="dash-empty">加载中 / 需配置代理</div>;
            if (logged.length === 0) {
              return (
                <div className="dash-empty" onClick={() => onNavigate('accounts')} style={{ cursor: 'pointer' }}>
                  去账号页登录后，这里看各平台粉丝 / 获赞 / 关注、增长趋势与最新笔记 →
                </div>
              );
            }
            const d = anaSel ? anaData[anaSel] : undefined;
            const WIN: [typeof anaWin, string][] = [
              ['last', '较上次'], ['day', '较昨日'], ['week', '较上周'], ['month', '较上月'], ['year', '较去年'],
            ];
            return (
              <>
                <div className="ana-plats">
                  {logged.map((p) => (
                    <button key={p.platform} className={`chip ${anaSel === p.platform ? 'active' : ''}`}
                      onClick={() => runAna(p.platform)}>{p.name}</button>
                  ))}
                </div>
                {!d && <div className="dash-empty">点上方平台查看该账号数据</div>}
                {d === 'loading' && (
                  <div className="loading" style={{ padding: '28px 0' }}><div className="spinner" />抓取中…正在校验登录态并读取平台数据</div>
                )}
                {d === 'error' && (
                  <div className="dash-empty" style={{ color: 'var(--red)' }}>{anaErrors[anaSel] || '抓取失败，请稍后重试'}；点平台重试</div>
                )}
                {d && d !== 'loading' && d !== 'error' && (!d.loggedIn ? (
                  <div className="dash-empty" onClick={() => onNavigate('accounts')} style={{ cursor: 'pointer' }}>
                    该平台登录态已失效，去账号页重登 →
                  </div>
                ) : (
                  <div className="ana-body">
                    {d.stale && (
                      <div className="dash-empty" role="status"
                        style={{ gridColumn: '1 / -1', color: 'var(--red)' }}>
                        刷新失败，显示旧数据 · 真实取数时间：{fetchedAtText(d.fetched_at)}
                      </div>
                    )}
                    {d.warning && <div className="dash-empty" role="status" style={{ gridColumn: '1 / -1' }}>{d.warning}</div>}
                    {/* 概览 + 增长对比 */}
                    <div className="ana-col ana-col-main">
                      <div className="ana-id">{d.nickname ? `@${d.nickname}` : d.name}</div>
                      <div className="ana-overview">
                        {([['粉丝', 'followers'], ['获赞', 'likes'], ['关注', 'following']] as const).map(([label, key]) => {
                          const w = d.growth?.[anaWin] ?? null;
                          const g = w ? growthInfo(w[key as 'followers' | 'likes']) : null;
                          return (
                            <div key={key} className="ana-stat">
                              <div className="ana-stat-val">{fmtNum(d[key])}</div>
                              <div className="ana-stat-label">{label}</div>
                              {g ? <div className="ana-stat-delta" style={{ color: g.color }}>{g.text}</div>
                                 : <div className="ana-stat-delta ana-muted">—</div>}
                            </div>
                          );
                        })}
                      </div>
                      <div className="ana-wins">
                        {WIN.map(([k, lab]) => (
                          <button key={k} className={`ana-win ${anaWin === k ? 'on' : ''}`}
                            onClick={() => setAnaWin(k)}>{lab}</button>
                        ))}
                      </div>
                      <div className="ana-wins-note">
                        {d.growth?.[anaWin]?.since_days != null
                          ? `对比 ${d.growth[anaWin]!.since_days} 天前的快照`
                          : '暂无该时段历史快照，多刷新几次即可积累对比'}
                      </div>
                    </div>

                    {/* 近7日平台指标 + 环比 */}
                    <div className="ana-col ana-col-metrics">
                      <div className="ana-sub">近 7 日 · 环比</div>
                      {(d.metrics ?? []).length === 0 ? (
                        <div className="dash-empty">该平台未提供近 7 日指标</div>
                      ) : (
                        <div className="ana-metrics">
                          {(d.metrics ?? []).map((m) => {
                            const vs = m.vs ?? '';
                            const up = vs.startsWith('+');
                            const has = vs && vs !== '-';
                            return (
                              <div key={m.label} className="ana-metric">
                                <div className="ana-metric-val">{m.value}</div>
                                <div className="ana-metric-label">{m.label}</div>
                                {has && <div className="ana-metric-vs" style={{ color: up ? 'var(--trend-up)' : 'var(--trend-down)' }}>环比{vs}</div>}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>

                    {/* 最新笔记（可点进原文） */}
                    <div className="ana-col ana-col-notes">
                      <div className="ana-sub">最新笔记</div>
                      {(d.notes ?? []).length === 0 ? (
                        <div className="dash-empty">该账号暂无可读取的已发布笔记</div>
                      ) : (
                        <div className="ana-notes">
                          {(d.notes ?? []).slice(0, 6).map((n, i) => (
                            <a key={i} className="ana-note" href={n.url} target="_blank" rel="noreferrer" title={n.title}>
                              {n.cover
                                ? <img className="ana-note-cover" src={n.cover} alt="" referrerPolicy="no-referrer" />
                                : <span className="ana-note-cover ana-note-cover-ph">📝</span>}
                              <span className="ana-note-main">
                                <span className="ana-note-title">{n.title || '(无标题)'}</span>
                                {n.stat && <span className="ana-note-stat">{n.stat}</span>}
                              </span>
                              <span className="ana-note-go">↗</span>
                            </a>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </>
            );
          })()}
        </div>
      </div>
    </div>
  );
}
