import { createContext, useContext } from 'react';
import { localWorkspaceData } from './workspaceData';
import type { WorkspaceData } from './workspaceData';

export const WorkspaceDataContext =
  createContext<WorkspaceData>(localWorkspaceData);

export function useWorkspaceData(): WorkspaceData {
  return useContext(WorkspaceDataContext);
}
