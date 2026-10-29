import { useDashboardNotifySocket } from "./useDashboardNotifySocket";

interface TeamBoardFrame {
  type: string;
  team_id: string;
}

const BOARD_FRAME_TYPES = new Set([
  "team.task.updated",
  "team.goal.updated",
  "team.runtime.updated",
  "team.decision.updated",
  "team.wait.updated",
]);

/**
 * Subscribe to team board/runtime events for one team and refetch on every
 * change. Frames are deliberately minimal — the page re-reads server state so
 * ordering, goals, runtime status and waits stay server-authoritative.
 */
export function useTeamTaskEvents(
  teamId: string | undefined,
  onChanged: () => void,
): void {
  useDashboardNotifySocket((raw) => {
    if (!teamId) return;
    const frame = raw as Partial<TeamBoardFrame>;
    if (!frame?.type || !BOARD_FRAME_TYPES.has(frame.type)) return;
    if (frame.team_id !== teamId) return;
    onChanged();
  });
}
