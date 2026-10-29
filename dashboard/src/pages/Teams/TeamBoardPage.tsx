import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router-dom";
import { Alert, Drawer, Popconfirm, Spin, Tag } from "antd";
import {
  DndContext,
  PointerSensor,
  pointerWithin,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  ArrowLeft,
  CircleCheck,
  OctagonX,
  Pause,
  Play,
  RotateCcw,
  Target,
} from "lucide-react";
import { message } from "@/utils/antdMessage";
import { goalsApi, type AgentGoal } from "../../api/modules/goals";
import { tasksApi, type AgentTask } from "../../api/modules/tasks";
import {
  teamsApi,
  type TeamRecord,
  type TeamRuntimeState,
  type TeamRuntimeWait,
} from "../../api/modules/teams";
import { apiErrorMessage } from "../../utils/apiError";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { useTeamTaskEvents } from "../../hooks/useTeamTaskEvents";
import GoalDrawer from "../../components/GoalDrawer";
import {
  formatMessageTime,
  formatServerDateTime,
} from "../../utils/formatMessageTime";
import { ExpertIcon } from "../Experts/components/iconForName";
import { TEAM_ICON_NAME, teamPortraitUrl } from "../../utils/teamAgent";
import styles from "./TeamBoardPage.module.less";

interface Lane {
  key: string;
  /** Task statuses rendered in this lane. */
  statuses: string[];
  /** Status written when a card is dropped here; null = not a drop target. */
  dropStatus: string | null;
}

const LANES: Lane[] = [
  {
    key: "proposed",
    statuses: ["proposed", "assigned"],
    dropStatus: "proposed",
  },
  { key: "running", statuses: ["running"], dropStatus: "running" },
  {
    key: "blocked",
    statuses: ["blocked", "needs_input"],
    dropStatus: "blocked",
  },
  { key: "review", statuses: ["review"], dropStatus: "review" },
  { key: "done", statuses: ["done"], dropStatus: "done" },
  { key: "closed", statuses: ["failed", "cancelled"], dropStatus: null },
];

const STATUS_TAG_COLOR: Record<string, string> = {
  proposed: "default",
  assigned: "blue",
  running: "processing",
  blocked: "warning",
  needs_input: "volcano",
  review: "purple",
  done: "success",
  failed: "error",
  cancelled: "default",
};

function laneOf(status: string): Lane {
  return LANES.find((lane) => lane.statuses.includes(status)) ?? LANES[0];
}

function statusTag(t: (key: string) => string, status: string) {
  return (
    <Tag
      color={STATUS_TAG_COLOR[status] ?? "default"}
      className={styles.statusTag}
    >
      {t(`teams.board.status.${status}`)}
    </Tag>
  );
}

function BoardCard({
  task,
  memberName,
  timeZone,
  onOpen,
}: {
  task: AgentTask;
  memberName: Map<string, string>;
  timeZone: string;
  onOpen: (task: AgentTask) => void;
}) {
  const { t } = useTranslation();
  const { attributes, listeners, setNodeRef, transform, isDragging } =
    useDraggable({ id: `task:${task.task_id}` });
  const assignee = task.assignee ? memberName.get(task.assignee) : undefined;
  return (
    <div
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      className={`${styles.card} ${isDragging ? styles.cardDragging : ""}`}
      style={
        transform
          ? { transform: `translate3d(${transform.x}px, ${transform.y}px, 0)` }
          : undefined
      }
      onClick={() => onOpen(task)}
    >
      <div className={styles.cardTitle} title={task.title}>
        {task.title}
      </div>
      {task.blocked_reason ? (
        <div className={styles.cardReason} title={task.blocked_reason}>
          {task.blocked_reason}
        </div>
      ) : null}
      {task.result_summary ? (
        <div className={styles.cardResult} title={task.result_summary}>
          {task.result_summary}
        </div>
      ) : null}
      <div className={styles.cardMeta}>
        {laneOf(task.status).statuses.length > 1
          ? statusTag(t, task.status)
          : null}
        {assignee ? (
          <span className={styles.cardAssignee}>{assignee}</span>
        ) : null}
        {task.active_wait_id ? (
          <span className={styles.cardWait}>
            {t("teams.board.waitingBadge")}
          </span>
        ) : null}
        {task.priority > 0 ? (
          <span className={styles.cardPriority}>P{task.priority}</span>
        ) : null}
        {task.due_at ? (
          <span className={styles.cardDue}>
            {formatMessageTime(task.due_at * 1000, timeZone)}
          </span>
        ) : null}
      </div>
    </div>
  );
}

function Lane({
  lane,
  tasks,
  memberName,
  timeZone,
  onOpen,
}: {
  lane: Lane;
  tasks: AgentTask[];
  memberName: Map<string, string>;
  timeZone: string;
  onOpen: (task: AgentTask) => void;
}) {
  const { t } = useTranslation();
  const { setNodeRef, isOver } = useDroppable({ id: `lane:${lane.key}` });
  return (
    <div
      ref={setNodeRef}
      className={`${styles.lane} ${
        isOver && lane.dropStatus ? styles.laneOver : ""
      }`}
    >
      <div className={styles.laneHeader}>
        <span>{t(`teams.board.lane.${lane.key}`)}</span>
        <span className={styles.laneCount}>{tasks.length}</span>
      </div>
      <div className={styles.laneBody}>
        {tasks.length === 0 ? (
          <div className={styles.laneEmpty}>{t("teams.board.emptyLane")}</div>
        ) : (
          tasks.map((task) => (
            <BoardCard
              key={task.task_id}
              task={task}
              memberName={memberName}
              timeZone={timeZone}
              onOpen={onOpen}
            />
          ))
        )}
      </div>
    </div>
  );
}

export default function TeamBoardPage() {
  const params = useParams<{ teamId?: string; agentId?: string }>();
  // One board for both: a team host dispatches rows, a digital employee
  // executes them itself. Only the goal/task calls differ in shape.
  const teamId = params.teamId ?? params.agentId;
  const isTeam = Boolean(params.teamId);
  const { t } = useTranslation();
  const navigate = useNavigate();
  const timeZone = useServerTimezone();

  const [team, setTeam] = useState<TeamRecord | null>(null);
  const [goal, setGoal] = useState<AgentGoal | null>(null);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [runtime, setRuntime] = useState<TeamRuntimeState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<AgentTask | null>(null);
  const [goalDrawerOpen, setGoalDrawerOpen] = useState(false);
  const [driveBusy, setDriveBusy] = useState(false);
  const [resolvingWaitId, setResolvingWaitId] = useState<string | null>(null);

  const memberName = useMemo(
    () =>
      new Map(
        (team?.members ?? []).map((member) => [member.agent_id, member.name]),
      ),
    [team],
  );

  const load = useCallback(
    async (silent = false) => {
      if (!teamId) return;
      if (!silent) setLoading(true);
      try {
        const [goals, taskRows] = await Promise.all([
          goalsApi.list(teamId),
          tasksApi.list(teamId),
        ]);
        setGoal(goals.find((item) => item.status === "active") ?? null);
        setTasks(taskRows);
        if (isTeam) {
          const [teamRow, runtimeRow] = await Promise.all([
            teamsApi.get(teamId),
            teamsApi.getRuntime(teamId),
          ]);
          setTeam(teamRow);
          setRuntime(runtimeRow);
        } else {
          setTeam(null);
          setRuntime(null);
        }
        setLoadError(null);
      } catch (err) {
        if (!silent) {
          setLoadError(apiErrorMessage(err, t("teams.board.loadFailed"), t));
        }
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [teamId, isTeam, t],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // §7.5 board events: refetch in the background so two dashboard sessions
  // (or the host agent itself) stay in sync without a manual reload.
  useTeamTaskEvents(teamId, () => {
    void load(true);
  });

  const patchStatus = useCallback(
    async (task: AgentTask, status: string) => {
      if (!teamId) return;
      try {
        const updated = await tasksApi.patch(teamId, task.task_id, {
          status,
          expected_version: task.version,
        });
        setTasks((prev) =>
          prev.map((item) =>
            item.task_id === updated.task_id ? updated : item,
          ),
        );
      } catch (err) {
        message.error(apiErrorMessage(err, t("teams.board.patchFailed"), t));
        void load(true);
      }
    },
    [teamId, t, load],
  );

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
  );

  const onDragEnd = useCallback(
    (event: DragEndEvent) => {
      const overId = event.over?.id;
      if (!overId) return;
      const lane = LANES.find((item) => item.key === String(overId).slice(5));
      const taskId = String(event.active.id).replace(/^task:/, "");
      const task = tasks.find((item) => item.task_id === taskId);
      if (!lane || lane.dropStatus === null || !task) return;
      if (task.status === lane.dropStatus) return;
      void patchStatus(task, lane.dropStatus);
    },
    [tasks, patchStatus],
  );

  const runDriveAction = useCallback(
    async (action: "start" | "pause" | "resume" | "finish") => {
      if (!teamId) return;
      setDriveBusy(true);
      try {
        if (action === "start") await teamsApi.startDrive(teamId);
        else if (action === "pause") await teamsApi.pauseDrive(teamId);
        else if (action === "resume") await teamsApi.resumeDrive(teamId);
        else await teamsApi.finishDrive(teamId);
        await load(true);
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("teams.board.runtime.actionFailed"), t),
        );
      } finally {
        setDriveBusy(false);
      }
    },
    [teamId, t, load],
  );

  const stopDrive = useCallback(async () => {
    if (!teamId) return;
    setDriveBusy(true);
    try {
      const result = await teamsApi.stopDrive(teamId);
      message.success(
        t("teams.board.stopDriveDone", {
          jobs: result.disabled_jobs,
          tasks: result.cancelled_tasks,
        }),
      );
      await load(true);
    } catch (err) {
      message.error(apiErrorMessage(err, t("teams.board.stopDriveFailed"), t));
    } finally {
      setDriveBusy(false);
    }
  }, [teamId, t, load]);

  const resolveApproval = useCallback(
    async (wait: TeamRuntimeWait, status: "approved" | "rejected") => {
      if (!teamId) return;
      setResolvingWaitId(wait.wait_id);
      try {
        await teamsApi.resolveWait(teamId, wait.wait_id, { status });
        message.success(t("teams.board.approval.done"));
        await load(true);
      } catch (err) {
        message.error(
          apiErrorMessage(err, t("teams.board.approval.failed"), t),
        );
        await load(true);
      } finally {
        setResolvingWaitId(null);
      }
    },
    [teamId, t, load],
  );

  if (loading && !team) {
    return (
      <div className={styles.center}>
        <Spin />
      </div>
    );
  }
  if (loadError) {
    return (
      <div className={styles.page}>
        <Alert type="error" showIcon message={loadError} />
      </div>
    );
  }

  const runtimeStatus = runtime?.status ?? "idle";
  const driveActive = [
    "running",
    "deciding",
    "dispatching",
    "waiting",
  ].includes(runtimeStatus);
  const drivePaused =
    runtimeStatus === "paused_user" || runtimeStatus === "paused_budget";
  const driveStartable =
    runtimeStatus === "idle" ||
    runtimeStatus === "stopped" ||
    runtimeStatus === "finishing";
  const approvalWaits = (runtime?.open_waits ?? []).filter(
    (wait) => wait.kind === "approval",
  );

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div className={styles.headerMain}>
          <button
            type="button"
            className={styles.backBtn}
            aria-label={t("common.back", "Back")}
            onClick={() => navigate("/experts")}
          >
            <ArrowLeft size={16} />
          </button>
          {team ? (
            <span className={styles.teamIcon}>
              <ExpertIcon
                iconUrl={teamPortraitUrl(team.icon_url)}
                iconName={TEAM_ICON_NAME}
                size={28}
              />
            </span>
          ) : null}
          <h1 className={styles.title}>{t("teams.board.title")}</h1>
          {team ? <span className={styles.teamName}>{team.name}</span> : null}
          <div className={styles.headerActions}>
            <button
              type="button"
              className={styles.actionBtn}
              onClick={() => setGoalDrawerOpen(true)}
            >
              <Target size={13} />
              {t("teams.board.goalSet")}
            </button>
            <Popconfirm
              title={t("teams.board.stopDriveConfirm")}
              onConfirm={() => void stopDrive()}
              okText={t("common.confirm", "OK")}
              cancelText={t("common.cancel")}
              okButtonProps={{ danger: true }}
            >
              <button
                type="button"
                className={`${styles.actionBtn} ${styles.stopBtn}`}
                disabled={driveBusy}
              >
                <OctagonX size={13} />
                {t("teams.board.stopDrive")}
              </button>
            </Popconfirm>
          </div>
        </div>
        <div className={styles.goalBar}>
          <Target size={13} className={styles.goalIcon} />
          {goal ? (
            <div className={styles.goalFields}>
              <div className={styles.goalRow}>
                <span className={styles.goalLabel}>
                  {t("teams.board.goalFormTitle")}
                </span>
                <span
                  className={`${styles.goalValue} ${styles.goalTitle}`}
                  title={goal.title}
                >
                  {goal.title}
                </span>
              </div>
              {goal.description ? (
                <div className={styles.goalRow}>
                  <span className={styles.goalLabel}>
                    {t("teams.board.goalFormDesc")}
                  </span>
                  <span className={styles.goalValue} title={goal.description}>
                    {goal.description}
                  </span>
                </div>
              ) : null}
              {goal.success_criteria ? (
                <div className={styles.goalRow}>
                  <span className={styles.goalLabel}>
                    {t("teams.board.goalFormCriteria")}
                  </span>
                  <span
                    className={styles.goalValue}
                    title={goal.success_criteria}
                  >
                    {goal.success_criteria}
                  </span>
                </div>
              ) : null}
            </div>
          ) : (
            <span className={styles.goalEmpty}>
              {t("teams.board.goalEmpty")}
            </span>
          )}
        </div>
        <div className={styles.runtimeBar}>
          <span
            className={`${styles.runtimeDot} ${
              driveActive ? styles.runtimeDotActive : ""
            }`}
          />
          <span className={styles.runtimeLabel}>
            {t(`teams.board.runtime.status.${runtimeStatus}`, runtimeStatus)}
          </span>
          {runtime?.open_waits.length ? (
            <span className={styles.runtimeMeta}>
              {t("teams.board.runtime.openWaits", {
                count: runtime.open_waits.length,
              })}
            </span>
          ) : null}
          <div className={styles.runtimeActions}>
            {driveActive ? (
              <>
                <button
                  type="button"
                  className={styles.actionBtn}
                  disabled={driveBusy}
                  onClick={() => void runDriveAction("pause")}
                >
                  <Pause size={13} />
                  {t("teams.board.runtime.pause")}
                </button>
                <button
                  type="button"
                  className={styles.actionBtn}
                  disabled={driveBusy}
                  onClick={() => void runDriveAction("finish")}
                >
                  <CircleCheck size={13} />
                  {t("teams.board.runtime.finish")}
                </button>
              </>
            ) : null}
            {drivePaused || driveStartable ? (
              <button
                type="button"
                className={styles.actionBtn}
                disabled={driveBusy}
                onClick={() =>
                  void runDriveAction(drivePaused ? "resume" : "start")
                }
              >
                {drivePaused ? <RotateCcw size={13} /> : <Play size={13} />}
                {drivePaused
                  ? t("teams.board.runtime.resume")
                  : t("teams.board.runtime.start")}
              </button>
            ) : null}
          </div>
        </div>
        {approvalWaits.length > 0 ? (
          <div className={styles.approvalBar}>
            <div className={styles.approvalTitle}>
              {t("teams.board.approval.title", { count: approvalWaits.length })}
            </div>
            {approvalWaits.map((wait) => {
              const task = wait.task_id
                ? tasks.find((item) => item.task_id === wait.task_id)
                : undefined;
              const hitl = Boolean(wait.external_ref);
              return (
                <div key={wait.wait_id} className={styles.approvalItem}>
                  <div className={styles.approvalText}>
                    <div className={styles.approvalTask}>
                      {task?.title ?? t("teams.board.approval.teamLevel")}
                    </div>
                    {wait.reason || !hitl ? (
                      <div className={styles.approvalMeta}>
                        {wait.reason || t("teams.board.approval.noReason")}
                      </div>
                    ) : null}
                  </div>
                  {hitl ? (
                    <span className={styles.approvalHint}>
                      {t("teams.board.approval.hitlHint")}
                    </span>
                  ) : (
                    <div className={styles.approvalActions}>
                      <button
                        type="button"
                        className={styles.actionBtn}
                        disabled={resolvingWaitId === wait.wait_id}
                        onClick={() => void resolveApproval(wait, "approved")}
                      >
                        <CircleCheck size={13} />
                        {t("teams.board.approval.approve")}
                      </button>
                      <Popconfirm
                        title={t("teams.board.approval.rejectConfirm")}
                        onConfirm={() => void resolveApproval(wait, "rejected")}
                        okText={t("common.confirm", "OK")}
                        cancelText={t("common.cancel")}
                        okButtonProps={{ danger: true }}
                      >
                        <button
                          type="button"
                          className={`${styles.actionBtn} ${styles.stopBtn}`}
                          disabled={resolvingWaitId === wait.wait_id}
                        >
                          <OctagonX size={13} />
                          {t("teams.board.approval.reject")}
                        </button>
                      </Popconfirm>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ) : null}
      </div>

      <DndContext
        sensors={sensors}
        collisionDetection={pointerWithin}
        onDragEnd={onDragEnd}
      >
        <div className={styles.lanes}>
          {LANES.map((lane) => (
            <Lane
              key={lane.key}
              lane={lane}
              tasks={tasks.filter((task) =>
                lane.statuses.includes(task.status),
              )}
              memberName={memberName}
              timeZone={timeZone}
              onOpen={setSelected}
            />
          ))}
        </div>
      </DndContext>

      <GoalDrawer
        open={goalDrawerOpen}
        agentId={teamId ?? null}
        agentKind="team"
        memberOptions={(team?.members ?? []).map((member) => ({
          id: member.agent_id,
          name: member.name,
        }))}
        onClose={() => setGoalDrawerOpen(false)}
        onChanged={() => void load(true)}
      />

      <Drawer
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.title}
        width={420}
      >
        {selected ? (
          <div className={styles.detail}>
            <div className={styles.detailRow}>
              {statusTag(t, selected.status)}
              {selected.priority > 0 ? (
                <span className={styles.cardPriority}>
                  P{selected.priority}
                </span>
              ) : null}
            </div>
            {selected.assignee ? (
              <div className={styles.detailItem}>
                <span className={styles.detailLabel}>
                  {t("teams.board.detailAssignee")}
                </span>
                <span>
                  {memberName.get(selected.assignee) ?? selected.assignee}
                </span>
              </div>
            ) : null}
            {selected.due_at ? (
              <div className={styles.detailItem}>
                <span className={styles.detailLabel}>
                  {t("teams.board.detailDue")}
                </span>
                <span>{formatServerDateTime(selected.due_at, timeZone)}</span>
              </div>
            ) : null}
            {selected.spec ? (
              <div className={styles.detailItem}>
                <span className={styles.detailLabel}>
                  {t("teams.board.detailSpec")}
                </span>
                <p className={styles.detailText}>{selected.spec}</p>
              </div>
            ) : null}
            {selected.blocked_reason ? (
              <div className={styles.detailItem}>
                <span className={styles.detailLabel}>
                  {t("teams.board.detailBlocked")}
                </span>
                <p className={styles.detailText}>{selected.blocked_reason}</p>
              </div>
            ) : null}
            {selected.result_summary ? (
              <div className={styles.detailItem}>
                <span className={styles.detailLabel}>
                  {t("teams.board.detailResult")}
                </span>
                <p className={styles.detailText}>{selected.result_summary}</p>
              </div>
            ) : null}
            <div className={styles.detailItem}>
              <span className={styles.detailLabel}>
                {t("teams.board.detailUpdated")}
              </span>
              <span>{formatServerDateTime(selected.updated_at, timeZone)}</span>
            </div>
          </div>
        ) : null}
      </Drawer>
    </div>
  );
}
