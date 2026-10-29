import type { ReactNode } from 'react';
import Sidebar from './Sidebar';
import type { Page } from './Sidebar';
import SubNav from './SubNav';
import type { ChatSession } from '../lib/store';
import type { PersonaItem } from '../lib/api';

const SUBNAV_PAGES: Page[] = [
  'trends', 'ideas', 'calendar', 'publish', 'breakdown',
];

export interface WorkspaceLayoutProps {
  currentPage: Page;
  onPageChange: (page: Page) => void;
  personas: PersonaItem[];
  selectedPersona: string;
  onPersonaChange: (persona: string) => void;
  onNewProfile: () => void;
  sessions: ChatSession[];
  activeSessionId: string | null;
  onSessionSelect: (id: string) => void;
  onSessionDelete: (id: string) => void;
  onSessionRename: (id: string, title: string) => void;
  onSessionArchive: (id: string, archived: boolean) => void;
  onNewChat: () => void;
  gatewayStatus: string;
  apiDirect: boolean;
  agentRuntime: string;
  onOpenSettings: () => void;
  mode?: 'local' | 'control';
  identityLabel?: string;
  onLogout?: () => void;
  personasEnabled?: boolean;
  showEmptySessions?: boolean;
  statusText?: string;
  statusOnline?: boolean;
  hasMoreSessions?: boolean;
  onLoadMoreSessions?: () => void;
  children: ReactNode;
}

/** 一套原布局：LocalApp 与 control WorkspaceApp 共用同一 Sidebar/主区/SubNav。 */
export default function WorkspaceLayout({
  currentPage,
  onPageChange,
  personas,
  selectedPersona,
  onPersonaChange,
  onNewProfile,
  sessions,
  activeSessionId,
  onSessionSelect,
  onSessionDelete,
  onSessionRename,
  onSessionArchive,
  onNewChat,
  gatewayStatus,
  apiDirect,
  agentRuntime,
  onOpenSettings,
  mode = 'local',
  identityLabel,
  onLogout,
  personasEnabled = true,
  showEmptySessions = false,
  statusText,
  statusOnline,
  hasMoreSessions = false,
  onLoadMoreSessions,
  children,
}: WorkspaceLayoutProps) {
  return (
    <div className="app-layout">
      <Sidebar
        currentPage={currentPage}
        onPageChange={onPageChange}
        personas={personas}
        selectedPersona={selectedPersona}
        onPersonaChange={onPersonaChange}
        onNewProfile={onNewProfile}
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSessionSelect={onSessionSelect}
        onSessionDelete={onSessionDelete}
        onSessionRename={onSessionRename}
        onSessionArchive={onSessionArchive}
        onNewChat={onNewChat}
        gatewayStatus={gatewayStatus}
        apiDirect={apiDirect}
        agentRuntime={agentRuntime}
        onOpenSettings={onOpenSettings}
        mode={mode}
        identityLabel={identityLabel}
        onLogout={onLogout}
        personasEnabled={personasEnabled}
        showEmptySessions={showEmptySessions}
        statusText={statusText}
        statusOnline={statusOnline}
        hasMoreSessions={hasMoreSessions}
        onLoadMoreSessions={onLoadMoreSessions}
      />
      <main className="main-content">
        {SUBNAV_PAGES.includes(currentPage) && (
          <SubNav current={currentPage} onNavigate={onPageChange} />
        )}
        <div className="page-host">{children}</div>
      </main>
    </div>
  );
}
