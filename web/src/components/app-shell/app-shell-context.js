import { createContext, useContext } from 'react';

export const AppShellContext = createContext({
  embedded: false,
  currentTab: '',
  onNavigate: () => {},
});

export function useAppShell() {
  return useContext(AppShellContext);
}
