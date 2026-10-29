import { App } from "antd";
import { Bell } from "lucide-react";
import { useRef } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { useDashboardNotifySocket } from "./useDashboardNotifySocket";
import { useAgent } from "../context/AgentContext";
import { ExpertIcon } from "../pages/Experts/components/iconForName";
import {
  emitSessionEvent,
  invalidateHistory,
} from "../pages/Chat/hooks/chatStore";
import {
  parseDashboardPushFrame,
  truncatePushText,
} from "../utils/dashboardPushToast";
import styles from "./useDashboardPushToast.module.less";

interface ApprovalRequiredFrame {
  type: "approval_required";
  agent_id: string;
  thread_id?: string;
  deep_link?: string;
}

function PushToastIcon({
  iconUrl,
  iconName,
}: {
  iconUrl?: string | null;
  iconName?: string | null;
}) {
  return (
    <span className={styles.icon}>
      {iconUrl || iconName ? (
        <ExpertIcon iconUrl={iconUrl} iconName={iconName} size={16} />
      ) : (
        <Bell size={16} />
      )}
    </span>
  );
}

/** Listen for dashboard text pushes and show a bottom-right toast. */
export function useDashboardPushToast(): void {
  const { notification } = App.useApp();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { refresh, agents } = useAgent();
  const agentsRef = useRef(agents);
  agentsRef.current = agents;
  const notificationRef = useRef(notification);
  notificationRef.current = notification;
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  const tRef = useRef(t);
  tRef.current = t;
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useDashboardNotifySocket((raw) => {
    const frame = raw as Partial<ApprovalRequiredFrame>;
    if (frame?.type === "approval_required") {
      showApprovalToast(frame as ApprovalRequiredFrame);
      return;
    }
    const parsed = parseDashboardPushFrame(raw);
    if (!parsed) return;
    void refreshRef.current({ silent: true });
    // A proactive run wrote to this thread outside the chat socket. Stale it
    // here (not in the chat page listener) so the cached page is dropped
    // even when the push lands while another route is open.
    invalidateHistory(parsed.thread_id);
    // Refresh the sidebar and the open thread's history without a reload.
    emitSessionEvent({
      kind: "sessionsChanged",
      sessionId: parsed.thread_id,
      agentId: parsed.agent_id,
    });
    const agent = agentsRef.current.find((a) => a.agent_id === parsed.agent_id);
    const accent = agent?.color?.trim() || "#6366f1";
    const title = parsed.agent_name
      ? tRef.current("chat.pushToast.title", { name: parsed.agent_name })
      : tRef.current("chat.pushToast.titleFallback");
    const noticeKey = `dash-push-${parsed.thread_id}-${Date.now()}`;
    notificationRef.current.open({
      key: noticeKey,
      placement: "bottomRight",
      className: styles.notice,
      style: {
        ["--push-accent" as string]: accent,
      },
      message: (
        <span className={styles.header}>
          <PushToastIcon
            iconUrl={agent?.icon_url}
            iconName={agent?.icon_name}
          />
          <span className={styles.title}>{title}</span>
        </span>
      ),
      description: (
        <span className={styles.body}>{truncatePushText(parsed.text)}</span>
      ),
      duration: null,
      closable: true,
      onClick: () => {
        notificationRef.current.destroy(noticeKey);
        navigateRef.current(`/chat/${parsed.agent_id}/${parsed.thread_id}`);
      },
    });
  });

  function showApprovalToast(frame: ApprovalRequiredFrame) {
    const agent = agentsRef.current.find((a) => a.agent_id === frame.agent_id);
    const accent = agent?.color?.trim() || "#6366f1";
    const title = agent?.name
      ? tRef.current("teams.approvalToast.title", { name: agent.name })
      : tRef.current("teams.approvalToast.titleFallback");
    const noticeKey = `approval-${frame.agent_id}-${Date.now()}`;
    notificationRef.current.open({
      key: noticeKey,
      placement: "bottomRight",
      className: styles.notice,
      style: {
        ["--push-accent" as string]: accent,
      },
      message: (
        <span className={styles.header}>
          <PushToastIcon
            iconUrl={agent?.icon_url}
            iconName={agent?.icon_name}
          />
          <span className={styles.title}>{title}</span>
        </span>
      ),
      description: (
        <span className={styles.body}>
          {tRef.current("teams.approvalToast.description")}
        </span>
      ),
      duration: null,
      closable: true,
      onClick: () => {
        notificationRef.current.destroy(noticeKey);
        navigateRef.current(
          frame.deep_link || `/chat/${frame.agent_id}/${frame.thread_id ?? ""}`,
        );
      },
    });
  }
}
