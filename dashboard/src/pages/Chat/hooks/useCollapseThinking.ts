import { useLocalStorageState } from "ahooks";

/** Solo / 1:1 chat preference (default: collapse while streaming). */
export const COLLAPSE_THINKING_SOLO_KEY = "octop:collapse-thinking";
/** Team room preference (default: collapse while streaming). */
export const COLLAPSE_THINKING_TEAM_KEY = "octop:collapse-thinking:team";

export function collapseThinkingStorageKey(isTeam: boolean): string {
  return isTeam ? COLLAPSE_THINKING_TEAM_KEY : COLLAPSE_THINKING_SOLO_KEY;
}

/**
 * Browser display preference for folding thinking/tool panels while streaming.
 * Solo and team chats both collapse by default so a long reasoning trace never
 * pushes the answer off the first screen. The two keys stay separate so
 * toggling one does not affect the other.
 */
export function useCollapseThinking(isTeam = false) {
  return useLocalStorageState<boolean>(collapseThinkingStorageKey(isTeam), {
    defaultValue: true,
    listenStorageChange: true,
  });
}
