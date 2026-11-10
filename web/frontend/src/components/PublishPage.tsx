import { useState, useRef, useEffect, useCallback } from 'react';
import type { AccountItem, OutputFile } from '../lib/api';
import { useWorkspaceData } from '../lib/workspaceDataContext';
import { isUncertainPublishError, workspaceErrorMessage } from '../lib/workspaceData';
import {
  clearPublishIntent, loadPublishDraft, loadPublishIntents,
  savePublishDraft, savePublishIntents,
} from '../lib/store';
import type { PublishIntentPayload } from '../lib/store';
import { decidePublishIntent, publishFingerprint } from '../lib/publishIntent';
import { renderMarkdown } from '../lib/sanitize';
import { IconPublish, IconCopy, IconCheck, IconCalendar, IconSkills, IconEdit, IconStop, IconTrash } from './icons';

interface PublishPageProps {
  persona: string;
}

// 平台列表须与后端 LOGIN_RUNNERS 对齐（有登录/发布链路的才列）
// draft: true = 该平台支持「存草稿」（落后台草稿箱，不公开发布）
const PLATFORMS: { key: string; label: string; titleLimit?: number; bodyLimit: number; hint: string; draft?: boolean }[] = [
  { key: 'xiaohongshu', label: '小红书', titleLimit: 20, bodyLimit: 1000, hint: '标题≤20，正文≤1000，重情绪+话题标签' },
  { key: 'douyin', label: '抖音', titleLimit: 55, bodyLimit: 55, hint: '文案≤55，前几字是钩子' },
  { key: 'kuaishou', label: '快手', titleLimit: 30, bodyLimit: 1000, hint: '视频或图片(图文)，标题≤30，需附媒体' },
  { key: 'weixin-channels', label: '视频号', bodyLimit: 1000, hint: '需附视频，短描述+话题标签，微信扫码登录' },
  { key: 'zhihu', label: '知乎', bodyLimit: 5000, hint: '长文/回答，讲清逻辑' },
  { key: 'juejin', label: '掘金', titleLimit: 100, bodyLimit: 20000, hint: '技术文章（Markdown），标题≤100；发布默认分类，标签需掘金已有标签' },
  { key: 'bilibili', label: 'B站', titleLimit: 80, bodyLimit: 2000, hint: '需附视频，标题≤80、简介≤2000，默认投「知识」分区' },
  { key: 'toutiao', label: '今日头条', titleLimit: 30, bodyLimit: 10000, draft: true, hint: '图文长文，标题≤30 字，话题≤5 个；附件图片插进正文，可存草稿' },
  { key: 'wechat-oa', label: '公众号', titleLimit: 64, bodyLimit: 20000, hint: '图文文章，正文用 Markdown，首图作封面，发到草稿箱；需先在账号页「登录公众号后台」扫码' },
  { key: 'twitter-x', label: '推特 X', bodyLimit: 280, hint: '标题与正文合并，最终长度以 X 校验为准；可附 1 图或 1 视频；窗口登录需要运行 Easel 的电脑有图形桌面及外网连接' },
];
const LABEL2KEY = Object.fromEntries(PLATFORMS.map((p) => [p.label, p.key]));

// 能一键发布的平台（有后端 publisher）
const PUBLISHABLE = new Set(['xiaohongshu', 'douyin', 'kuaishou', 'weixin-channels', 'zhihu', 'juejin', 'bilibili', 'wechat-oa', 'twitter-x', 'toutiao']);
// 必须附带媒体的平台（无媒体发不了）——公众号需要一张封面图，也计入；X 纯文字推文合法，不计入
const MEDIA_REQUIRED = new Set(['xiaohongshu', 'douyin', 'kuaishou', 'weixin-channels', 'bilibili', 'wechat-oa']);
// 只能发视频的平台（抖音/视频号/B站：图文不走此链路，必须视频）
const VIDEO_ONLY = new Set(['douyin', 'weixin-channels', 'bilibili']);
const VIDEO_RE = /\.(mp4|mov|webm|mkv|avi|m4v|flv|ts)$/i;

function parseSections(text: string): Record<string, string> {
  const parts = text.split(/^\s*={2,}\s*(.+?)\s*={2,}\s*$/m);
  const map: Record<string, string> = {};
  for (let i = 1; i < parts.length; i += 2) map[parts[i].trim()] = (parts[i + 1] || '').trim();
  return map;
}

type PubState = { status: 'publishing' | 'ok' | 'fail' | 'unknown'; msg: string };

type PublishPayload = PublishIntentPayload;

function newPublishRequestId(platform: string): string {
  return `publish-${platform}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** 首次发送前固定意图并落盘；同稿重试/刷新复用，新稿才换新号。 */
function rememberPublishIntent(
  scope: string, platform: string, payload: PublishPayload,
  requestId: string,
): void {
  const intents = loadPublishIntents(scope);
  intents[platform] = {
    requestId,
    fingerprint: publishFingerprint(platform, payload),
    payload,
  };
  savePublishIntents(intents, scope);
}

export default function PublishPage({ persona }: PublishPageProps) {
  const data = useWorkspaceData();
  const draftScope = data.publishDraftScope;
  const draft0 = loadPublishDraft(draftScope);
  // 草稿是否真的保存过媒体/画像：旧版草稿没有字段，刷新后只能视为丢失。
  const mediaPersisted = useRef(Array.isArray(draft0.media));
  const personaPersisted = useRef(typeof draft0.persona === 'string');
  // 本页生命周期内用户是否显式改过媒体/画像；只有丢失且没动过才恢复旧稿。
  const mediaEdited = useRef(false);
  const personaEdited = useRef(false);
  const lastPersonaProp = useRef(persona);
  const [publishPersona, setPublishPersona] = useState(
    () => persona || draft0.persona || '');
  const [title, setTitle] = useState(draft0.title);
  const [body, setBody] = useState(draft0.body);
  const [platforms, setPlatforms] = useState<string[]>(draft0.platforms);
  const [overrides, setOverrides] = useState<Record<string, string>>(draft0.overrides);
  const [tags, setTags] = useState(draft0.tags || '');
  // 存草稿模式：只对声明了 draft 能力的平台（今日头条）生效，其余平台仍真发布
  const [asDraft, setAsDraft] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [copied, setCopied] = useState('');
  const [toast, setToast] = useState('');
  const [adapting, setAdapting] = useState(false);
  const [checking, setChecking] = useState(false);
  const [checkResult, setCheckResult] = useState('');
  const adaptCtl = useRef<{ abort(): void } | null>(null);
  const adaptRun = useRef('');
  // 每个平台当前发布的 Run；旧轮询/短信回应不得写入新任务。
  const publishRuns = useRef<Record<string, string>>({});
  // 异步发布轮询的 interval 集合：离开页面统一清掉，否则最长还会继续转 15 分钟
  const pollTimers = useRef<Set<ReturnType<typeof setInterval>>>(new Set());
  const aliveRef = useRef(true);

  // 发布相关
  const [accounts, setAccounts] = useState<AccountItem[]>([]);
  const [mediaFiles, setMediaFiles] = useState<OutputFile[]>([]);
  // 刷新后同时恢复可见选择，避免“页面显示空、实际提交旧图”。
  const [selectedMedia, setSelectedMedia] = useState<string[]>(
    () => draft0.media ?? []);
  const [showPicker, setShowPicker] = useState(false);
  const [pub, setPub] = useState<Record<string, PubState>>({});
  const [publishing, setPublishing] = useState(false);
  // 发布时的短信验证窗口（抖音风控条件触发；没触发就不弹）
  const [pubSms, setPubSms] = useState<{ platform: string; name: string; state: string; message: string; runId: string } | null>(null);
  const [pubSmsCode, setPubSmsCode] = useState('');
  const [pubSmsBusy, setPubSmsBusy] = useState(false);

  // 草稿持久化：任何改动即写 localStorage，切页/刷新回来都在。
  // 旧版草稿没有媒体/画像字段；用户没动过时不要抢先写空值，否则会
  // 把“刷新丢失”误标成“用户主动清空”。
  useEffect(() => {
    savePublishDraft({
      title, body, platforms, overrides, tags,
      media: mediaPersisted.current || mediaEdited.current
        ? selectedMedia : undefined,
      persona: personaPersisted.current || personaEdited.current
        ? publishPersona : undefined,
    }, draftScope);
  }, [title, body, platforms, overrides, tags, selectedMedia, publishPersona,
    draftScope]);

  // 画像来自应用级选择：挂载后发生的切换视为显式改稿，刷新恢复不算。
  useEffect(() => {
    if (persona === lastPersonaProp.current) return;
    lastPersonaProp.current = persona;
    personaEdited.current = true;
    setPublishPersona(persona);
  }, [persona]);

  const cleanupPublishActivity = useCallback(() => {
    // 卸载：中止流 + 停掉所有发布状态轮询（否则在后台空转最长 15 分钟）
    aliveRef.current = false;
    adaptCtl.current?.abort();
    pollTimers.current.forEach((iv) => clearInterval(iv));
    pollTimers.current.clear();
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    return cleanupPublishActivity;
  }, [cleanupPublishActivity]);

  // 登录态 + 可选媒体列表
  useEffect(() => {
    data.fetchAccounts().then(setAccounts).catch(() => { /* 忽略 */ });
    data.fetchOutputs().then((roots) => {
      const files: OutputFile[] = [];
      const walk = (n: OutputFile) => {
        if (n.type === 'file') { if (n.kind === 'image' || n.kind === 'video') files.push(n); return; }
        for (const c of n.children || []) walk(c);
      };
      roots.forEach(walk);
      files.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
      setMediaFiles(files);
    }).catch(() => { /* 忽略 */ });
  }, [data]);

  const loginOf = (key: string) => accounts.find((a) => a.platform === key)?.loggedIn ?? false;

  const toggle = (k: string) =>
    setPlatforms((prev) => prev.includes(k) ? prev.filter((x) => x !== k) : [...prev, k]);
  const showToast = (m: string) => { setToast(m); setTimeout(() => setToast(''), 2800); };
  const effective = (k: string) => overrides[k] ?? body;
  const empty = !title.trim() && !body.trim();
  const isVideoPath = (p: string) => /\.(mp4|mov|flv|mkv|avi|webm|m4v|wmv|ts|mpe?g)$/i.test(p);
  const toggleMedia = (path: string) =>
    setSelectedMedia((prev) => {
      if (prev.includes(path)) {
        mediaEdited.current = true;
        return prev.filter((x) => x !== path);
      }
      // 通用规则：图片和视频不能同时；视频一次只发一个
      if (isVideoPath(path)) {
        if (prev.length && !prev.every(isVideoPath)) { showToast('图片和视频不能同时发布，请先取消已选图片'); return prev; }
        mediaEdited.current = true;
        return [path]; // 视频单选
      }
      if (prev.some(isVideoPath)) { showToast('图片和视频不能同时发布，请先取消已选视频'); return prev; }
      mediaEdited.current = true;
      return [...prev, path]; // 图片可多选（图文）
    });

  // A. 智能一稿多改（流式：逐字改写，直接流进每个平台卡片）
  const adapt = () => {
    if (empty || platforms.length === 0 || adapting) return;
    const sel = PLATFORMS.filter((p) => platforms.includes(p.key));
    const prompt =
      `请执行 /skill-content-repurposing：把下面这条内容改编到这些平台：${sel.map((p) => p.label).join('、')}。` +
      `务必参考该 SKILL 的 platform-specs 与改写配方，贴合各平台原生格式、语气与字数。\n` +
      `【硬性要求】输出各平台“可直接复制发布的纯文本正文”，禁止任何 Markdown 语法：不要 **加粗**、# 标题、---、表格、代码块、编号列表符号；` +
      `小红书可用 emoji 和 #话题标签，按平台习惯自然分行即可。\n` +
      `严格只按下面格式输出、每个平台之间用分隔线，不要任何额外说明：\n` +
      sel.map((p) => `===${p.label}===\n<该平台纯文本正文>`).join('\n') +
      `\n\n原始内容：\n标题：${title}\n正文：${body}`;

    setAdapting(true);
    adaptRun.current = '';
    let acc = '';
    const base = { ...overrides };
    adaptCtl.current = data.adaptContent(prompt, persona, {
      onToken: (chunk) => {             // 逐字：实时解析并流进对应平台卡片
        acc += chunk;
        const map = parseSections(acc);
        const next = { ...base };
        for (const [label, text] of Object.entries(map)) {
          const key = LABEL2KEY[label];
          if (key && platforms.includes(key)) next[key] = text;
        }
        setOverrides(next);
      },
      onDone: () => {                   // 完成
        const hit = Object.keys(parseSections(acc)).length;
        setAdapting(false);
        showToast(hit ? `已生成 ${hit} 个平台版本` : '未能解析，可重试');
      },
      onError: (error) => {
        setAdapting(false);
        showToast(workspaceErrorMessage(error, '改写失败，请重试'));
      },
      onAdmission: (runId) => { adaptRun.current = runId; },
    });
  };
  const stopAdapt = () => {
    adaptCtl.current?.abort();
    const runId = adaptRun.current;
    adaptRun.current = '';
    if (runId) void data.stopTask(runId).catch(() => { /* 保留原提示 */ });
    setAdapting(false);
  };

  const performPrecheck = async () => {
    const prompt =
      `你是社媒发布审核助手。针对下面这条待发内容做两项检查，用简洁中文分点输出：\n` +
      `1. **合规风险**：是否含极限词/医疗功效/敏感或违规表述，列出问题词+替换建议；无则写"未见明显风险"。\n` +
      `2. **标题/钩子**：给标题打 1-10 分，并给 1-2 个更好的备选。\n` +
      `最后一行给「✅可发 / ⚠️建议修改」结论。\n\n待检内容：\n标题：${title}\n正文：${body}`;
    const content = `待发布内容：\n标题：${title}\n正文：${body}`;
    const [general, personaResult] = await Promise.all([
      data.runAgent(prompt),
      persona ? data.executeSkill('persona-check', content, persona) : Promise.resolve(null),
    ]);
    return `${general.response}\n\n---\n\n## 人设一致性\n\n${personaResult?.response || '未选择画像，已跳过人设一致性检查。'}`;
  };

  // C. 发布前一键预检
  const check = async () => {
    if (empty) return;
    setChecking(true); setCheckResult('');
    try {
      setCheckResult(await performPrecheck());
    } catch (e) {
      setCheckResult(e instanceof Error ? e.message : '预检失败');
    } finally { setChecking(false); }
  };

  // D. 一键发布（真发布，二次确认）
  const publishAll = async () => {
    if (empty || publishing || checking) return;
    const targets = PLATFORMS.filter((p) => platforms.includes(p.key) && PUBLISHABLE.has(p.key));
    if (targets.length === 0) {
      showToast('所选平台暂不支持一键发布（B站请用「复制」或终端 biliup）');
      return;
    }
    setChecking(true);
    try {
      setCheckResult(await performPrecheck());
    } catch (e) {
      setCheckResult(`预检失败：${e instanceof Error ? e.message : '未知错误'}\n\n预检仅用于提醒，不会阻止你继续发布。`);
    } finally {
      setChecking(false);
    }
    // 先固定每个平台要提交的稿件。显式改过媒体/画像时按当前可见选择
    // 新建请求；只有刷新/切身份丢失选择且用户没动过，才复用落盘旧稿。
    const intentScope = data.publishDraftScope;
    const provenance = {
      mediaPersisted: mediaPersisted.current,
      personaPersisted: personaPersisted.current,
      mediaEdited: mediaEdited.current,
      personaEdited: personaEdited.current,
      appPersona: publishPersona,
    };
    const plans = targets.map((t) => {
      const payload: PublishPayload = {
        title, body: effective(t.key), media: selectedMedia, tags,
        persona: publishPersona,
      };
      const prior = intentScope
        ? loadPublishIntents(intentScope)[t.key] : undefined;
      const decision = decidePublishIntent(
        prior, t.key, payload, provenance);
      return {
        target: t,
        payload: decision.payload,
        reuse: decision.action === 'reuse',
        requestId: decision.action === 'reuse'
          ? decision.requestId : newPublishRequestId(t.key),
        prior,
      };
    });
    const unresolved = plans.filter((plan) => plan.prior && !plan.reuse);
    const submitLines = plans.map((plan) =>
      `- ${plan.target.label}：素材 ${
        plan.payload.media.length ? plan.payload.media.join('、') : '无'}` +
      `；画像 ${plan.payload.persona || '无'}`).join('\n');
    const okToSend = window.confirm(
      `发布前预检已执行，结果已显示在页面中。人设评分只做提醒，不会阻止发布。\n\n` +
      `即将【真实发布】到：${targets.map((t) => t.label).join('、')}\n` +
      `${submitLines}\n` +
      (unresolved.length
        ? `\n注意：${unresolved.map((plan) => plan.target.label).join('、')}` +
          `有一笔旧发布请求结果尚未确认。本次将按当前页面显示的稿件` +
          `新建请求；若旧请求已被平台接纳，可能造成重复发布。` +
          `如不确定，请先取消并核对旧请求状态后再发布。\n`
        : '') +
      `\n这会公开发布到你的账号，确定继续？`);
    if (!okToSend) return;

    setPublishing(true);
    for (const plan of plans) {
      const { target: t, payload, requestId } = plan;
      if (!loginOf(t.key)) {
        setPub((r) => ({ ...r, [t.key]: { status: 'fail', msg: '未登录 · 去账号页登录' } }));
        continue;
      }
      const pendingMedia = payload.media;
      if (MEDIA_REQUIRED.has(t.key) && pendingMedia.length === 0) {
        setPub((r) => ({ ...r, [t.key]: { status: 'fail', msg: '需附带图片/视频' } }));
        continue;
      }
      if (VIDEO_ONLY.has(t.key)
          && !pendingMedia.some((p) => VIDEO_RE.test(p))) {
        setPub((r) => ({ ...r, [t.key]: { status: 'fail', msg: `${t.label}只能发视频，请从内容库选一个视频` } }));
        continue;
      }
      // 首次发送前就固定意图并落盘；接纳结果不明时刷新/重试仍用同一个
      // requestId 与落盘 payload，服务端据此返回同一个 Run，不会开启
      // 第二次外部动作。显式改稿时替换旧意图，旧回包按 requestId 丢弃。
      if (intentScope && !plan.reuse) {
        rememberPublishIntent(intentScope, t.key, payload, requestId);
      }
      setPub((r) => ({ ...r, [t.key]: { status: 'publishing', msg: '发布中…可能需 1-2 分钟' } }));
      try {
        const res = await data.publishNow(
          t.key, payload, { requestId });
        publishRuns.current[t.key] = res.runId || '';
        if (res.async) {
          // 所有平台都先接纳后轮询；短信只绑定当前 Run。
          await pollAsyncPublish(t.key, t.label,
            intentScope ? requestId : undefined, intentScope);
        } else {
          const status = res.state === 'success'
            ? 'ok' : res.state === 'unknown' ? 'unknown' : 'fail';
          if (intentScope) {
            clearPublishIntent(t.key, intentScope, requestId);
          }
          setPub((r) => ({
            ...r,
            [t.key]: {
              status,
              msg: status === 'ok'
                ? (res.calendarRecorded === false
                  ? '已发布 ✅（日历记录失败，平台结果不受影响）'
                  : '已发布 ✅')
                : status === 'unknown'
                  ? (res.message || '发布结果未知，请到平台核对，勿自动重发')
                  : (res.detail || res.message || '发布失败'),
            },
          }));
        }
      } catch (e) {
        if (intentScope && isUncertainPublishError(e)) {
          // 响应丢失时服务端可能已接纳：保留意图，显示未知/核对中，
          // 不自动补发；用户再次点击会重试同一请求。
          setPub((r) => ({
            ...r,
            [t.key]: {
              status: 'unknown',
              msg: '网络中断或服务暂不可用，发布结果未知；再次点击「一键发布」会核对/重试同一请求，不会重复提交。',
            },
          }));
        } else {
          setPub((r) => ({
            ...r,
            [t.key]: {
              status: 'fail',
              msg: workspaceErrorMessage(e, '发布失败'),
            },
          }));
        }
      }
    }
    setPublishing(false);
    showToast('发布流程结束，见各平台卡片状态');
  };

  // 异步发布轮询：直到 success/error/unknown；只接受当前 Run 的回应。
  const pollAsyncPublish = (
    key: string, label: string, requestId?: string, intentScope?: string,
  ) => new Promise<void>((resolve) => {
    const started = Date.now();
    const expectedRun = publishRuns.current[key] || '';
    const iv = setInterval(async () => {
      if (!aliveRef.current) { clearInterval(iv); pollTimers.current.delete(iv); resolve(); return; }
      if (Date.now() - started > 15 * 60 * 1000) {   // 15min 兜底
        clearInterval(iv); pollTimers.current.delete(iv); setPubSms(null);
        setPub((r) => ({ ...r, [key]: { status: 'unknown', msg: '发布超时，平台结果未知，请到平台核对' } }));
        resolve(); return;
      }
      let s;
      try {
        s = await data.publishStatus(
          key, requestId ? { requestId } : undefined);
      } catch { return; }  // 单次失败忽略
      if (!aliveRef.current) return;
      // 同一平台已换成新意图：旧请求的迟到回应不得覆盖新卡片或清除新意图。
      if (requestId && intentScope) {
        const current = loadPublishIntents(intentScope)[key];
        if (!current || current.requestId !== requestId) {
          clearInterval(iv);
          pollTimers.current.delete(iv);
          resolve();
          return;
        }
      }
      if (expectedRun && s.runId && s.runId !== expectedRun) return;  // 迟到旧回应
      const runId = s.runId || expectedRun;
      if (s.state === 'sms_required' || s.state === 'verifying') {
        setPubSms({ platform: key, name: label, state: s.state,
                    message: s.message, runId });
        setPub((r) => ({ ...r, [key]: { status: 'publishing', msg: s.message || '需短信验证' } }));
      } else if (s.state === 'success') {
        clearInterval(iv); pollTimers.current.delete(iv); setPubSms(null);
        if (intentScope && requestId) {
          clearPublishIntent(key, intentScope, requestId);
        }
        setPub((r) => ({ ...r, [key]: {
          status: 'ok',
          msg: s.calendarRecorded === false
            ? '已发布 ✅（日历记录失败，平台结果不受影响）'
            : '已发布 ✅',
        } }));
        resolve();
      } else if (s.state === 'error') {
        clearInterval(iv); pollTimers.current.delete(iv); setPubSms(null);
        if (intentScope && requestId) {
          clearPublishIntent(key, intentScope, requestId);
        }
        setPub((r) => ({ ...r, [key]: { status: 'fail', msg: s.message || '发布失败' } }));
        resolve();
      } else if (s.state === 'unknown' && !data.localSettingsEnabled) {
        clearInterval(iv); pollTimers.current.delete(iv); setPubSms(null);
        if (intentScope && requestId) {
          clearPublishIntent(key, intentScope, requestId);
        }
        setPub((r) => ({ ...r, [key]: {
          status: 'unknown',
          msg: s.message || '发布结果未知，请到平台核对，勿自动重发',
        } }));
        resolve();
      } else {
        setPub((r) => ({ ...r, [key]: { status: 'publishing', msg: s.message || '发布中…' } }));
      }
    }, 2500);
    pollTimers.current.add(iv);
  });

  // 刷新后按本人当前 Run 恢复卡片/短信/轮询；只读状态，不创建新任务。
  useEffect(() => {
    let active = true;
    const restore = async () => {
      const intentScope = data.publishDraftScope;
      const intents = intentScope ? loadPublishIntents(intentScope) : {};
      for (const key of draft0.platforms) {
        if (!PUBLISHABLE.has(key)) continue;
        const runBefore = publishRuns.current[key] || '';
        const intent = intentScope ? intents[key] : undefined;
        try {
          // 有未确认意图时精确查询该请求的原 Run，避免把上次已完成任务
          // 误当成这次请求的结果。
          const s = await data.publishStatus(
            key, intent ? { requestId: intent.requestId } : undefined);
          if (!active) return;
          // 恢复期间用户已开始新发布：旧状态不能覆盖新任务。
          if ((publishRuns.current[key] || '') !== runBefore) continue;
          if (intent) {
            const current = loadPublishIntents(intentScope)[key];
            if (!current || current.requestId !== intent.requestId) continue;
          }
          const runId = s.runId || '';
          if (!runId) {
            if (intent) {
              // 未确认的接纳结果：保留意图，可重试同一请求，不自动补发。
              setPub((r) => ({ ...r, [key]: {
                status: 'unknown',
                msg: '上次发布请求尚未确认，可再次点击「一键发布」核对/重试同一请求。',
              } }));
            }
            continue;
          }
          publishRuns.current[key] = runId;
          const label = PLATFORMS.find((p) => p.key === key)?.label || key;
          if (s.state === 'success') {
            if (intent) {
              clearPublishIntent(key, intentScope, intent.requestId);
            }
            setPub((r) => ({ ...r, [key]: {
              status: 'ok',
              msg: s.calendarRecorded === false
                ? '已发布 ✅（日历记录失败，平台结果不受影响）'
                : '已发布 ✅',
            } }));
          } else if (s.state === 'error') {
            if (intent) {
              clearPublishIntent(key, intentScope, intent.requestId);
            }
            setPub((r) => ({ ...r, [key]: { status: 'fail', msg: s.message || '发布失败' } }));
          } else if (s.state === 'unknown') {
            // 精确查询已找到终态 Run：本轮意图已有确定结果。
            if (intent) {
              clearPublishIntent(key, intentScope, intent.requestId);
            }
            setPub((r) => ({ ...r, [key]: {
              status: 'unknown',
              msg: s.message || '发布结果未知，请到平台核对，勿自动重发',
            } }));
          } else {
            setPub((r) => ({ ...r, [key]: { status: 'publishing', msg: s.message || '发布中…' } }));
            if (s.state === 'sms_required' || s.state === 'verifying') {
              setPubSms({ platform: key, name: label, state: s.state,
                          message: s.message, runId });
            }
            void pollAsyncPublish(key, label, intent?.requestId, intentScope);
          }
        } catch { /* 暂时不可用：保留重试，不创建新任务 */ }
      }
    };
    void restore();
    return () => { active = false; };
    // 只在挂载时恢复一次；platforms 以草稿初始值为准。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const stopPublish = async (key: string) => {
    const runId = publishRuns.current[key];
    if (!runId) return;
    // 先如实标为“未知”：停止只代表本地执行被要求停下，不能撤销已发生的
    // 外部发布，也不能把停止请求的失败伪装成平台失败。
    setPubSms(null);
    setPub((r) => ({ ...r, [key]: {
      status: 'unknown',
      msg: '已停止本地执行；平台结果未知，请到平台核对',
    } }));
    try {
      await data.stopTask(runId);
    } catch (error) {
      // 迟到停止失败不能覆盖已经开始的另一条发布。
      if (publishRuns.current[key] !== runId) return;
      showToast(workspaceErrorMessage(error, '停止发布失败'));
      setPub((r) => ({ ...r, [key]: {
        status: 'unknown',
        msg: '停止请求未完成，平台结果未知，请到平台核对',
      } }));
    }
  };

  const submitPubSms = async () => {
    if (!pubSms) return;
    const code = pubSmsCode.replace(/\D/g, '');
    if (code.length < 4) { showToast('验证码应为 4-6 位数字'); return; }
    setPubSmsBusy(true);
    try {
      const target = { platform: pubSms.platform, runId: pubSms.runId };
      await data.submitPublishSms(
        pubSms.platform, code, { runId: pubSms.runId || undefined });
      setPubSmsCode('');
      setPubSms((p) => p && p.platform === target.platform
        && p.runId === target.runId
        ? { ...p, state: 'verifying', message: '正在验证验证码…' }
        : p);
    } catch (e) {
      showToast(workspaceErrorMessage(e, '提交失败'));
    } finally {
      setPubSmsBusy(false);
    }
  };

  const copyFor = (key: string) => {
    const text = (title ? title + '\n\n' : '') + effective(key);
    navigator.clipboard?.writeText(text);
    setCopied(key); setTimeout(() => setCopied(''), 1400);
  };
  const addToCalendar = async (key: string) => {
    if (empty) return;
    // 用本地日期（日历页按本地渲染）：toISOString 是 UTC，东八区 0-8 点会落到「昨天」
    const d = new Date();
    const date = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    try {
      await data.createSchedule({
        title: title.trim() || effective(key).slice(0, 20), date,
        platform: PLATFORMS.find((p) => p.key === key)?.label || '',
        time: '', status: 'draft', note: effective(key),
      });
      showToast('已存为草稿并加入今天的日历');
    } catch (error) {
      showToast(workspaceErrorMessage(error, '保存失败'));
    }
  };

  const canPublish = platforms.some((k) => PUBLISHABLE.has(k));
  // 已选平台里支持存草稿的（目前只有今日头条）——没有就不显示开关，避免误导
  const draftCapable = PLATFORMS.filter((p) => platforms.includes(p.key) && p.draft);

  return (
    <div className="publish-page">
      <div className="publish-editor">
        <h1 className="page-title"><IconPublish size={21} /> 发布中心</h1>
        <p className="page-subtitle">一次编辑 → AI 一键改写成各平台版本 → 预检 → 附媒体 → 一键真发布。</p>

        <label className="field-label">标题</label>
        <input className="field" value={title} placeholder="标题（部分平台需要）"
          onChange={(e) => setTitle(e.target.value)} />
        <label className="field-label">正文（母版）</label>
        <textarea className="field" style={{ minHeight: 180 }} value={body}
          placeholder="写下你的内容，右侧按各平台规则实时预览；点「一键适配」让 AI 分平台改写…"
          onChange={(e) => setBody(e.target.value)} />

        <label className="field-label">话题标签 <span style={{ color: 'var(--text-secondary)', fontWeight: 400, fontSize: 12 }}>（逗号分隔，如「AI,职场,干货」；小红书会用 # 联想真正绑定话题）</span></label>
        <input className="field" value={tags} placeholder="AI,职场,干货"
          onChange={(e) => setTags(e.target.value)} />

        <label className="field-label">发布平台</label>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {PLATFORMS.map((p) => (
            <button key={p.key} className={`chip ${platforms.includes(p.key) ? 'active' : ''}`}
              onClick={() => toggle(p.key)}>{p.label}</button>
          ))}
        </div>
        {draftCapable.length > 0 && (
          <label className="draft-toggle">
            <input type="checkbox" checked={asDraft} onChange={(e) => setAsDraft(e.target.checked)} />
            <span>存草稿（{draftCapable.map((p) => p.label).join('、')} 进平台草稿箱，不公开发布；同批次的其它平台仍会真发布）</span>
          </label>
        )}

        <label className="field-label" style={{ marginTop: 14 }}>
          媒体附件 {selectedMedia.length > 0 && <span className="pv-badge">{selectedMedia.length} 个</span>}
          <span style={{ color: 'var(--text-secondary)', fontWeight: 400, fontSize: 12 }}>（小红书/抖音/快手/微信视频号/B站必需，从内容库选；抖音、视频号、B站须为视频；今日头条选中的图会插进正文）</span>
        </label>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <button className="btn btn-sm" onClick={() => setShowPicker((v) => !v)}>
            <IconSkills size={13} /> {showPicker ? '收起' : '选择媒体'}
          </button>
          {selectedMedia.map((path) => (
            <div key={path} className="media-chip" onClick={() => toggleMedia(path)} title="点击移除">
              {mediaFiles.find((f) => f.path === path)?.kind === 'image'
                ? <img src={data.mediaUrl(path)} alt="" /> : <span className="media-vid">🎬</span>}
              <span className="media-x">×</span>
            </div>
          ))}
        </div>
        {showPicker && (
          <div className="media-grid">
            {mediaFiles.length === 0 && <div className="dash-empty">内容库暂无图片/视频</div>}
            {mediaFiles.slice(0, 40).map((f) => (
              <div key={f.path}
                className={`media-cell ${selectedMedia.includes(f.path) ? 'sel' : ''}`}
                onClick={() => toggleMedia(f.path)} title={f.path}>
                {f.kind === 'image'
                  ? <img src={data.mediaUrl(f.path)} alt={f.name} loading="lazy" />
                  : <span className="media-vid">🎬<br />{f.name.slice(0, 12)}</span>}
                {selectedMedia.includes(f.path) && <span className="media-check">✓</span>}
              </div>
            ))}
          </div>
        )}

        <div className="publish-actions">
          {adapting ? (
            <button className="btn btn-sm" onClick={stopAdapt}><IconStop size={13} /> 停止生成</button>
          ) : (
            <button className="btn btn-sm btn-primary" disabled={empty || platforms.length === 0} onClick={adapt}>
              <IconSkills size={14} /> 一键适配各平台
            </button>
          )}
          <button className="btn btn-sm" disabled={empty || checking || adapting} onClick={check}>
            <IconCheck size={14} /> {checking ? '预检中…' : '发布前预检'}
          </button>
          <button className="btn btn-sm" disabled={empty} onClick={() => addToCalendar(platforms[0] || 'xiaohongshu')}>
            <IconCalendar size={14} /> 存草稿并排期
          </button>
          <button className="btn btn-sm btn-primary" disabled={empty || publishing || checking || !canPublish}
            title={canPublish ? '真实发布到已登录平台' : '所选平台无一键发布（B站走终端 biliup）'}
            onClick={publishAll}>
            <IconPublish size={14} /> {publishing ? '发布中…' : '一键发布'}
          </button>
          <button className="btn btn-sm btn-ghost" disabled={empty || adapting}
            onClick={() => { setTitle(''); setBody(''); setTags(''); setOverrides({}); setCheckResult(''); setPub({}); showToast('已清空'); }}>
            <IconTrash size={13} /> 清空
          </button>
        </div>
        {adapting && <div className="adapt-hint"><span className="live-pulse" />AI 正在逐字改写各平台版本…可随时停止。</div>}
        <p className="publish-saved-note">草稿已自动保存，切换页面/刷新回来内容都在。一键发布仅对「已登录 + 媒体齐全」的平台生效。</p>
        {checkResult && (
          <div className="panel" style={{ marginTop: 14 }}>
            <div className="panel-title"><IconCheck size={14} /> 发布前预检</div>
            <div className="skill-body-md" dangerouslySetInnerHTML={{ __html: renderMarkdown(checkResult) }} />
          </div>
        )}
      </div>

      <div className="publish-previews">
        {platforms.length === 0 && <div className="dash-empty">选择至少一个平台查看预览</div>}
        {PLATFORMS.filter((p) => platforms.includes(p.key)).map((p) => {
          const text = effective(p.key);
          const over = text.length > p.bodyLimit;
          const titleOver = p.titleLimit != null && title.length > p.titleLimit;
          const isEdit = editing === p.key;
          const ps = pub[p.key];
          const publishable = PUBLISHABLE.has(p.key);
          const logged = loginOf(p.key);
          return (
            <div key={p.key} className={`card pv-card pv-${p.key}`}>
              <div className="pv-head">
                <span className="pv-plat">
                  {p.label}
                  {overrides[p.key] != null && <span className="pv-badge">AI 版</span>}
                  {publishable && (logged
                    ? <span className="pv-badge pv-badge-ok">已登录</span>
                    : <span className="pv-badge">未登录</span>)}
                </span>
                <span className={`pv-count ${over ? 'over' : ''}`}>{text.length}/{p.bodyLimit}</span>
              </div>
              <div className="pv-body">
                {p.titleLimit != null && (
                  <div className={`pv-title ${titleOver ? 'over' : ''}`}>{title || <span className="pv-ph">标题…</span>}</div>
                )}
                {isEdit
                  ? <textarea className="field" style={{ minHeight: 120 }} value={text} autoFocus
                      onChange={(e) => setOverrides((o) => ({ ...o, [p.key]: e.target.value }))} />
                  : <div className="pv-text">{text || <span className="pv-ph">正文预览…</span>}{adapting && overrides[p.key] != null && <span className="streaming-cursor" />}</div>}
              </div>
              {ps && (
                <div className={`pv-pubstate ${ps.status}`}>
                  {ps.status === 'publishing' && <span className="live-pulse" />}
                  {ps.status === 'ok' ? '✅ ' : ps.status === 'unknown' ? '❓ ' : ps.status === 'fail' ? '⚠️ ' : ''}{ps.msg}
                </div>
              )}
              <div className="pv-foot">
                <span className="pv-hint">{p.hint}{over ? ' · 已超字数' : ''}</span>
                <div style={{ display: 'flex', gap: 6 }}>
                  <button className="pv-copy" onClick={() => setEditing(isEdit ? null : p.key)}>
                    <IconEdit size={13} />{isEdit ? '完成' : '编辑'}
                  </button>
                  <button className="pv-copy" onClick={() => copyFor(p.key)}>
                    {copied === p.key ? <IconCheck size={13} /> : <IconCopy size={13} />}{copied === p.key ? '已复制' : '复制'}
                  </button>
                  {ps?.status === 'publishing' && publishRuns.current[p.key] && (
                    <button className="pv-copy" onClick={() => { void stopPublish(p.key); }}>
                      停止本地执行
                    </button>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {toast && <div className="toast ok"><span className="toast-icon">✓</span>{toast}</div>}

      {pubSms && (
        <div className="overlay" onClick={(e) => { if (e.target === e.currentTarget) setPubSms(null); }}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 380 }}>
            <h3 style={{ margin: '0 0 4px' }}>发布验证 · {pubSms.name}</h3>
            <p style={{ fontSize: 13, color: /错误|过期|失败|重新|未完成|不正确|失效/.test(pubSms.message || '') ? 'var(--red)' : 'var(--text-secondary)' }}>
              {pubSms.message || '平台风控要求短信验证，验证码已发到你手机，请输入：'}
            </p>
            {pubSms.state === 'verifying' ? (
              <div className="dash-empty" style={{ padding: 16 }}>正在验证验证码…</div>
            ) : (
              <>
                <input inputMode="numeric" autoFocus
                  placeholder="请输入手机收到的验证码" value={pubSmsCode}
                  onChange={(e) => setPubSmsCode(e.target.value.replace(/\D/g, '').slice(0, 8))}
                  onKeyDown={(e) => { if (e.key === 'Enter') submitPubSms(); }}
                  style={{ width: '100%', boxSizing: 'border-box', textAlign: 'center',
                    letterSpacing: 6, fontSize: 20, padding: '10px 12px', margin: '4px 0 10px',
                    border: '1px solid var(--border)', borderRadius: 8 }} />
                <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                  <button className="btn btn-sm btn-ghost" onClick={() => setPubSms(null)}>关闭</button>
                  <button className="btn btn-sm btn-primary" disabled={pubSmsBusy} onClick={submitPubSms}>
                    {pubSmsBusy ? '提交中…' : '提交验证码'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
