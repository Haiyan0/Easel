import { useState } from 'react';
import ChatPage from './ChatPage';
import ControlSettingsPage from './ControlSettingsPage';
import AdminSettingsPage from './AdminSettingsPage';
import WorkspaceLayout from './WorkspaceLayout';
import type { Page } from './Sidebar';
import type { AuthContext, IdentityEpoch } from '../lib/auth';
import { workspaceModeFeatures } from '../lib/workspaceClient';
import { useWorkspace } from '../hooks/useWorkspace';
import type { ChatQuestion } from '../lib/api';

interface WorkspaceAppProps {
  ctx: AuthContext;
  epoch: number;
  epochs: IdentityEpoch;
  onExpired: () => void;
  onLogout: () => void;
}

const PAGE_LABELS: Partial<Record<Page, string>> = {
  dashboard: '工作台',
  trends: '热点雷达',
  ideas: '选题库',
  calendar: '内容日历',
  publish: '发布中心',
  breakdown: '爆款拆解',
  skills: '技能库',
  outputs: '内容库',
  accounts: '账号',
  profile: '画像',
};

function UnavailablePage({ page, onBack }: {
  page: Page; onBack: () => void;
}) {
  const label = PAGE_LABELS[page] || '该页面';
  return (
    <section className="unavailable-page" role="status">
      <h2>{label}暂未接通</h2>
      <p>
        登录工作区目前只开放原对话页。该页面仍在后续版本接入，
        不会读取本机数据，也不会显示假空列表。
      </p>
      <button className="btn btn-primary" onClick={onBack}>返回对话</button>
    </section>
  );
}

function EmptyChat({ onNewChat }: { onNewChat: () => void }) {
  return (
    <section className="unavailable-page" role="status">
      <h2>还没有会话</h2>
      <p>新建一个会话开始对话。</p>
      <button className="btn btn-primary" onClick={onNewChat}>新建对话</button>
    </section>
  );
}

export default function WorkspaceApp({
  ctx, epoch, epochs, onExpired, onLogout,
}: WorkspaceAppProps) {
  const [currentPage, setCurrentPage] = useState<Page>('chat');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<
    'personal' | 'defaults' | 'members'>('personal');
  const workspace = useWorkspace({ ctx, epoch, epochs, onExpired });
  const features = workspaceModeFeatures('control');
  const admin = ctx.role === 'admin';

  const openSettings = () => {
    setSettingsOpen(true);
    setSettingsTab('personal');
  };

  const navigate = (page: Page) => {
    setSettingsOpen(false);
    setCurrentPage(page);
  };

  const newChat = () => {
    setSettingsOpen(false);
    setCurrentPage('chat');
    void workspace.newChat();
  };

  const selectSession = (id: string) => {
    setSettingsOpen(false);
    setCurrentPage('chat');
    workspace.selectSession(id);
  };

  const deleteSession = (id: string) => {
    if (!window.confirm('删除该会话？服务端软删除，历史不再展示。')) return;
    void workspace.deleteSession(id);
  };

  const answer = (
    question: ChatQuestion, answers: Record<string, string[]>,
  ) => workspace.answer(question, answers);

  const notice = [workspace.historyError, workspace.notice]
    .filter(Boolean).join('；');

  const renderContent = () => {
    if (currentPage !== 'chat') {
      return (
        <UnavailablePage page={currentPage}
          onBack={() => setCurrentPage('chat')} />
      );
    }
    if (workspace.loading && !workspace.activeSession) {
      return (
        <section className="unavailable-page" role="status">
          <h2>正在加载会话…</h2>
        </section>
      );
    }
    if (!workspace.activeSession) {
      return <EmptyChat onNewChat={newChat} />;
    }
    return (
      <ChatPage
        key={`${epoch}:${workspace.activeSession.id}`}
        apiDirect={false}
        session={workspace.activeSession}
        stream={workspace.stream}
        onSend={(text) => { void workspace.send(text); }}
        onStop={() => { void workspace.stop(); }}
        mode="control"
        attachmentsEnabled={features.attachments}
        identityKey={`${epoch}:${ctx.userId}:${ctx.workspaceId}`}
        notice={notice}
        retryAvailable={!!workspace.failedTurnId}
        onRetrySend={() => { void workspace.retryFailed(); }}
        reconnectAvailable={workspace.canReconnect}
        onReconnect={workspace.reconnect}
        hasOlder={workspace.hasOlder}
        loadingOlder={workspace.loadingOlder}
        onLoadOlder={() => { void workspace.loadOlder(); }}
        onAnswerQuestion={answer}
      />
    );
  };

  const settingsContent = admin && settingsTab === 'defaults'
    ? <AdminSettingsPage ctx={ctx} call={workspace.call} section="defaults" />
    : admin && settingsTab === 'members'
      ? <AdminSettingsPage ctx={ctx} call={workspace.call} section="members" />
      : <ControlSettingsPage ctx={ctx} call={workspace.call} />;

  return (
    <>
    <WorkspaceLayout
      currentPage={currentPage}
      onPageChange={navigate}
      personas={[]}
      selectedPersona=""
      onPersonaChange={() => { /* control 画像未接通 */ }}
      onNewProfile={() => { /* control 画像未接通 */ }}
      sessions={workspace.sessions}
      activeSessionId={workspace.activeId}
      onSessionSelect={selectSession}
      onSessionDelete={deleteSession}
      onSessionRename={(id, title) => {
        void workspace.renameSession(id, title);
      }}
      onSessionArchive={(id, archived) => {
        void workspace.archiveSession(id, archived);
      }}
      onNewChat={newChat}
      gatewayStatus="connected"
      apiDirect={false}
      agentRuntime="control"
      onOpenSettings={openSettings}
      mode="control"
      identityLabel={`${admin ? '管理员' : '成员'}账户`}
      onLogout={onLogout}
      personasEnabled={features.personas}
      showEmptySessions
      statusText="已登录 · 服务端会话"
      statusOnline
      hasMoreSessions={workspace.sessionNextOffset !== null}
      onLoadMoreSessions={() => { void workspace.loadMoreSessions(); }}
    >
      {renderContent()}
    </WorkspaceLayout>
    {settingsOpen && (
      <div className="settings-overlay"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) setSettingsOpen(false);
        }}>
        <div className="settings-panel" role="dialog" aria-modal="true"
          aria-label="设置">
          <div className="settings-head">
            <div>
              <h2 className="settings-title">设置</h2>
              <div className="settings-sub">
                {admin
                  ? '个人覆盖 · 全体默认 · 成员与邀请'
                  : '个人覆盖 · 能力模型'}
              </div>
            </div>
            <div className="settings-actions">
              <button className="settings-close" title="关闭"
                onClick={() => setSettingsOpen(false)}>✕</button>
            </div>
          </div>
          <div className="settings-body">
            <nav className="settings-nav" role="tablist"
              aria-label="设置分类" aria-orientation="vertical">
              <button role="tab" aria-selected={settingsTab === 'personal'}
                className={`snav${settingsTab === 'personal' ? ' active' : ''}`}
                onClick={() => setSettingsTab('personal')}>
                个人设置<small>能力模型</small>
              </button>
              {admin && (
                <button role="tab" aria-selected={settingsTab === 'defaults'}
                  className={`snav${settingsTab === 'defaults' ? ' active' : ''}`}
                  onClick={() => setSettingsTab('defaults')}>
                  全体默认<small>主聊天</small>
                </button>
              )}
              {admin && (
                <button role="tab" aria-selected={settingsTab === 'members'}
                  className={`snav${settingsTab === 'members' ? ' active' : ''}`}
                  onClick={() => setSettingsTab('members')}>
                  成员与邀请<small>账号</small>
                </button>
              )}
            </nav>
            <div className="settings-main">{settingsContent}</div>
          </div>
          <div className="settings-foot">
            设置作用于当前工作区；个人覆盖不会改变其他成员。
          </div>
        </div>
      </div>
    )}
    </>
  );
}
