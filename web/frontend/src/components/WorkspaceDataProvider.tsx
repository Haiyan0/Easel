import type { ReactNode } from 'react';
import type { WorkspaceData } from '../lib/workspaceData';
import { WorkspaceDataContext } from '../lib/workspaceDataContext';

export default function WorkspaceDataProvider({ value, children }: {
  value: WorkspaceData; children: ReactNode;
}) {
  return (
    <WorkspaceDataContext.Provider value={value}>
      {children}
    </WorkspaceDataContext.Provider>
  );
}
