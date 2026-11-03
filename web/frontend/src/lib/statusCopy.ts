export type GatewayStatus = 'connected' | 'connecting' | 'disconnected';

export interface WorkspaceStatusView {
  gateway: boolean;
  state: string;
}

/** 受管工作区状态到页面连接状态的唯一映射。 */
export function workspaceGatewayStatus(
  status?: WorkspaceStatusView | null,
): GatewayStatus {
  if (!status) return 'connecting';
  if (status.gateway) return 'connected';
  if (status.state === 'stopping' || status.state === 'unavailable') return 'disconnected';
  return 'connecting';
}

/**
 * 首页问候语。受管模式不得提示用户安装 CLI：control 是服务端组件，
 * 用户只应看到工作区是否就绪以及模型可用性需要实际发消息验证。
 */
export function dashboardHeroText(
  gatewayStatus: string,
  apiDirect: boolean,
  agentRuntime: string,
): string {
  if (apiDirect) {
    return gatewayStatus === 'connected'
      ? '仅聊天模式已配置，可开始对话。'
      : '请先配置仅聊天模式的模型 API。';
  }
  if (agentRuntime === 'control') {
    return gatewayStatus === 'connected'
      ? '受管工作区已就绪；模型可用性请发送消息验证。'
      : '受管工作区尚未就绪，请稍后重试或查看工作区状态。';
  }
  if (agentRuntime !== 'openclaw') {
    return gatewayStatus === 'connected'
      ? `${agentRuntime} CLI 已安装；原生登录及模型可用性请发送消息验证。`
      : `请先在服务器安装并配置 ${agentRuntime} CLI。`;
  }
  return gatewayStatus === 'connected' ? '一切就绪。' : '⚠ 网关未连接。';
}

/** 侧栏状态文案；受管模式只描述工作区，不暴露宿主 CLI 概念。 */
export function sidebarStatusText(
  gatewayStatus: string,
  apiDirect: boolean,
  agentRuntime: string,
): string {
  if (gatewayStatus === 'connected') {
    if (apiDirect) return '仅聊天模式已配置';
    if (agentRuntime === 'control') return '受管工作区已就绪';
    return agentRuntime !== 'openclaw' ? `${agentRuntime} CLI 已安装` : '网关已连接';
  }
  if (gatewayStatus === 'disconnected') {
    if (apiDirect) return 'API 配置不完整';
    if (agentRuntime === 'control') return '受管工作区未就绪';
    return agentRuntime !== 'openclaw' ? `${agentRuntime} CLI 未配置` : '网关离线';
  }
  return agentRuntime === 'control' ? '受管工作区连接中…' : '连接中…';
}
