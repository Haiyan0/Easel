import type { PublishIntent, PublishIntentPayload } from './store';

export type PublishPayload = PublishIntentPayload;

/** 稿件版本指纹：同平台同内容才复用同一个请求号。 */
export function publishFingerprint(
  platform: string, payload: PublishPayload,
): string {
  return JSON.stringify([
    platform, payload.title, payload.body, payload.media, payload.tags,
    payload.persona,
  ]);
}

/**
 * 草稿能否证明当前媒体/画像就是用户最近一次看见的选择。
 *
 * 旧版草稿没有 media/persona 字段，刷新后页面只能显示空选择；此时若
 * 用户本页也没有动过这两项，才是“恢复丢失”而不是“主动改稿”。
 */
export interface DraftProvenance {
  mediaPersisted: boolean;
  personaPersisted: boolean;
  mediaEdited: boolean;
  personaEdited: boolean;
  /** 当前页面实际使用的画像；非空且与旧稿不同，说明用户已另选画像。 */
  appPersona: string;
}

export type PublishIntentDecision =
  | { action: 'reuse'; requestId: string; payload: PublishPayload }
  | { action: 'replace'; payload: PublishPayload }
  | { action: 'start'; payload: PublishPayload };

function sameList(left: string[], right: string[]): boolean {
  return left.length === right.length
    && left.every((value, index) => value === right[index]);
}

/**
 * 决定本次发布复用哪个意图。
 *
 * - 完整指纹一致：同一稿件，复用原请求号与落盘 payload。
 * - 标题/正文/标签变了：另一篇稿，新建请求。
 * - 媒体/画像不同：只有草稿从未保存过该项且用户本页未编辑时，才视为
 *   刷新/切身份丢失并恢复原 payload；否则是显式改稿，必须按当前可见
 *   稿件新建请求，不能静默提交旧素材。
 */
export function decidePublishIntent(
  prior: PublishIntent | undefined,
  platform: string,
  payload: PublishPayload,
  provenance: DraftProvenance,
): PublishIntentDecision {
  if (!prior) return { action: 'start', payload };
  if (prior.fingerprint === publishFingerprint(platform, payload)) {
    return {
      action: 'reuse',
      requestId: prior.requestId,
      payload: prior.payload ?? payload,
    };
  }
  if (!prior.payload
      || prior.payload.title !== payload.title
      || prior.payload.body !== payload.body
      || prior.payload.tags !== payload.tags) {
    return { action: 'replace', payload };
  }
  const mediaLost = !provenance.mediaPersisted && !provenance.mediaEdited;
  // 画像没有随草稿保存时，页面仍可能从应用级选择拿到一个非空值；
  // 只要它与旧稿不同，就是用户当前明确选中的画像，不能当作丢失恢复。
  const personaChosen = provenance.personaEdited
    || (provenance.appPersona !== ''
        && provenance.appPersona !== prior.payload.persona);
  const personaLost = !provenance.personaPersisted && !personaChosen;
  const mediaSame = sameList(prior.payload.media, payload.media);
  const personaSame = prior.payload.persona === payload.persona;
  if ((mediaSame || mediaLost) && (personaSame || personaLost)) {
    return {
      action: 'reuse',
      requestId: prior.requestId,
      payload: prior.payload,
    };
  }
  return { action: 'replace', payload };
}
