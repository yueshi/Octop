import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Spin, Tooltip } from "antd";
import {
  AppWindow,
  ArrowUpRight,
  Check,
  ChevronDown,
  MessageSquare,
  Plus,
  RefreshCw,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import type { AppSummary } from "../../api/modules/apps";
import {
  octopThreadsApi,
  type OctopThread,
} from "../../api/modules/octopThreads";
import type { HitlDecisionHandler } from "../../api/types/hitl";
import type { OctopAgent } from "../../context/AgentContext";
import { useAgent } from "../../context/AgentContext";
import { useServerTimezone } from "../../hooks/useServerTimezone";
import { message } from "../../utils/antdMessage";
import { isAgentChatReady } from "../../utils/agentError";
import { formatMessageTime } from "../../utils/formatMessageTime";
import { isTeamAgent } from "../../utils/teamAgent";
import ChatInput, { type ChatInputHandle } from "../Chat/components/ChatInput";
import MessageList from "../Chat/components/MessageList";
import { useChat, type ChatAttachment } from "../Chat/hooks/useChat";
import { parseConversationMode } from "../Chat/utils/conversationMode";
import { findPendingAsk, hasPendingHitl } from "../Chat/utils/pendingHitl";
import { formatThreadTitle } from "../Chat/utils/threadTitle";
import { ExpertIcon } from "../Experts/components/iconForName";
import { addDesktopNotification } from "./desktopNotifications";
import type { DesktopChatSeed } from "./desktopWindows";
import {
  APP_ACTIONS,
  actionPromptFor,
  fillTemplate,
  type AppActionDef,
} from "./appActions";
import styles from "./index.module.less";

interface DesktopChatWindowProps {
  agentId: string;
  agentName: string;
  agent?: OctopAgent | null;
  apps: AppSummary[];
  /** The MiniApp this chat was opened for (desktop action / app compose). */
  initialAppId?: string | null;
  /** One-shot composer prefill from the desktop pet or an action button. */
  seed?: DesktopChatSeed | null;
  onSeedConsumed?: () => void;
  /** Open or focus the MiniApp window inside the current desktop. */
  onOpenApp?: (app: AppSummary) => void;
  /** Desktop window id, used to focus this chat from a notification. */
  windowId?: string;
  /** True when this chat window is focused and on the current desktop. */
  active?: boolean;
}

type ChatPanel = "threads" | "apps" | null;

const THREAD_STORE_PREFIX = "octop:apps-desktop-thread:";

function threadTitle(thread: OctopThread | null, fallback: string): string {
  return formatThreadTitle(thread?.title) || fallback;
}

function rememberThread(agentId: string, threadId: string): void {
  try {
    localStorage.setItem(`${THREAD_STORE_PREFIX}${agentId}`, threadId);
  } catch {
    // Private windows can disable storage; the window still works for now.
  }
}

function recallThread(agentId: string): string | null {
  try {
    return localStorage.getItem(`${THREAD_STORE_PREFIX}${agentId}`);
  } catch {
    return null;
  }
}

/**
 * A complete conversation surface that lives inside a desktop window.
 *
 * It reuses the production chat transport (`useChat` → chatStore/WebSocket),
 * message renderer and composer, then adds desktop-app chrome: expert identity,
 * thread history, app context with expert actions, and a status footer.
 */
export default function DesktopChatWindow({
  agentId,
  agentName,
  agent = null,
  apps,
  initialAppId = null,
  seed = null,
  onSeedConsumed,
  onOpenApp,
  windowId,
  active = true,
}: DesktopChatWindowProps) {
  const { t } = useTranslation();
  const { agents } = useAgent();
  const timeZone = useServerTimezone();
  const tr = useCallback(
    (key: string, fallback: string) => t(key, fallback),
    [t],
  );

  const [threads, setThreads] = useState<OctopThread[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [threadsLoading, setThreadsLoading] = useState(true);
  const [threadsError, setThreadsError] = useState(false);
  const [bootKey, setBootKey] = useState(0);
  const [panel, setPanel] = useState<ChatPanel>(null);
  const [activeAppId, setActiveAppId] = useState<string | null>(initialAppId);
  const creatingRef = useRef(false);
  const inputRef = useRef<ChatInputHandle | null>(null);
  const seedAppliedRef = useRef<number | null>(null);
  const wasStreamingRef = useRef(false);

  const activeApp = apps.find((app) => app.app_id === activeAppId) ?? null;

  useEffect(() => {
    if (initialAppId) setActiveAppId(initialAppId);
  }, [initialAppId]);

  useEffect(() => {
    if (seed?.appId) setActiveAppId(seed.appId);
  }, [seed]);

  useEffect(() => {
    if (!panel) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setPanel(null);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [panel]);

  useEffect(() => {
    if (activeAppId && !apps.some((app) => app.app_id === activeAppId)) {
      setActiveAppId(null);
    }
  }, [activeAppId, apps]);

  const createThread = useCallback(async (): Promise<string | null> => {
    if (creatingRef.current) return null;
    creatingRef.current = true;
    try {
      const created = await octopThreadsApi.create(agentId);
      const now = Math.floor(Date.now() / 1000);
      rememberThread(agentId, created.thread_id);
      setThreadsError(false);
      setThreads((prev) => [
        {
          thread_id: created.thread_id,
          title: null,
          channel_type: "dashboard",
          session_key: created.session_key,
          last_active: 0,
          created_at: now,
          has_messages: false,
        },
        ...prev.filter((row) => row.thread_id !== created.thread_id),
      ]);
      setThreadId(created.thread_id);
      return created.thread_id;
    } catch {
      setThreadsError(true);
      message.error(
        tr(
          "apps.desktop.chatCreateFailed",
          "Could not create a conversation. Try again.",
        ),
      );
      return null;
    } finally {
      creatingRef.current = false;
    }
  }, [agentId, tr]);

  useEffect(() => {
    let alive = true;
    setThreadsLoading(true);
    setThreadsError(false);
    octopThreadsApi
      .list(agentId, 30)
      .then((rows) => {
        if (!alive) return;
        setThreads(rows);
        const rememberedId = recallThread(agentId);
        const preferred =
          (rememberedId
            ? rows.find((row) => row.thread_id === rememberedId)
            : undefined) ??
          rows.find(
            (row) => row.channel_type === "dashboard" && row.has_messages,
          ) ??
          rows.find((row) => row.channel_type === "dashboard") ??
          null;
        if (preferred) {
          setThreadId(preferred.thread_id);
          rememberThread(agentId, preferred.thread_id);
          setThreadsLoading(false);
          return;
        }
        void createThread().finally(() => {
          if (alive) setThreadsLoading(false);
        });
      })
      .catch(() => {
        if (!alive) return;
        setThreadsError(true);
        setThreadsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [agentId, bootKey, createThread]);

  const {
    messages,
    isStreaming,
    thinkingStartedAt,
    historyLoading,
    historyError,
    historyHasMore,
    historyLoadingMore,
    historyRefreshing,
    historyHydrated,
    runUsage,
    contextUsage,
    liveSpeakers,
    sendMessage,
    editAndResend,
    cancelStream,
    loadHistory,
    loadMoreHistory,
    refreshHistory,
    retryHistory,
    resumeHitl,
  } = useChat(threadId, agentId, isTeamAgent(agent));

  useEffect(() => {
    if (!threadId) return;
    void loadHistory(threadId);
  }, [loadHistory, threadId]);

  const refreshThreads = useCallback(async () => {
    try {
      const rows = await octopThreadsApi.list(agentId, 30);
      setThreads(rows);
    } catch {
      // Keep the locally known list; the next turn or panel open retries.
    }
  }, [agentId]);

  useEffect(() => {
    if (wasStreamingRef.current && !isStreaming) {
      void refreshThreads();
    }
    wasStreamingRef.current = isStreaming;
  }, [isStreaming, refreshThreads]);

  const activeThread =
    threads.find((row) => row.thread_id === threadId) ?? null;
  const title = threadTitle(
    activeThread,
    tr("apps.desktop.untitledThread", "New conversation"),
  );
  const conversationMode =
    activeThread?.conversation_mode ??
    parseConversationMode(agent?.config?.conversation_mode);
  const hitlPolicy = activeThread?.hitl_policy ?? null;

  const chatReady = agent ? isAgentChatReady(agent.state, agent) : true;
  const stateKey = isStreaming
    ? "streaming"
    : chatReady
    ? "running"
    : agent?.state || "offline";
  const statusLabel = isStreaming
    ? tr("apps.desktop.generating", "Replying…")
    : chatReady
    ? tr("apps.desktop.online", "Online")
    : tr(
        `common.agentState.${agent?.state ?? "stopped"}`,
        agent?.state || tr("apps.desktop.offline", "Offline"),
      );

  useEffect(() => {
    if (!seed || !threadId) return;
    if (seedAppliedRef.current === seed.id) return;
    const frame = requestAnimationFrame(() => {
      if (!inputRef.current) return;
      seedAppliedRef.current = seed.id;
      inputRef.current.setPrefillText(seed.text);
      inputRef.current.focusComposer();
      onSeedConsumed?.();
    });
    return () => cancelAnimationFrame(frame);
  }, [onSeedConsumed, seed, threadId]);

  const prefill = useCallback((text: string) => {
    inputRef.current?.setPrefillText(text);
    inputRef.current?.focusComposer();
  }, []);

  const pendingApproval = useMemo(
    () => hasPendingHitl(messages) || Boolean(findPendingAsk(messages)),
    [messages],
  );
  const hadPendingApprovalRef = useRef(false);
  useEffect(() => {
    if (pendingApproval && !hadPendingApprovalRef.current) {
      addDesktopNotification({
        kind: "approval",
        title: tr(
          "apps.desktop.notifications.approvalTitle",
          "Expert needs your confirmation",
        ),
        body: fillTemplate(
          tr(
            "apps.desktop.notifications.approvalBody",
            "{{name}} is waiting for your confirmation.",
          ),
          { name: agentName },
        ),
        windowId,
        dedupeKey: `approval:${windowId ?? agentId}:${threadId ?? ""}`,
      });
    }
    hadPendingApprovalRef.current = pendingApproval;
  }, [agentId, agentName, pendingApproval, threadId, tr, windowId]);

  const replyStreamingRef = useRef(isStreaming);
  useEffect(() => {
    const wasStreaming = replyStreamingRef.current;
    replyStreamingRef.current = isStreaming;
    if (wasStreaming && !isStreaming && !active && messages.length > 0) {
      addDesktopNotification({
        kind: "success",
        title: tr("apps.desktop.notifications.replyTitle", "Expert replied"),
        body: fillTemplate(
          tr(
            "apps.desktop.notifications.replyBody",
            "{{name}} replied in a conversation.",
          ),
          { name: agentName },
        ),
        windowId,
      });
    }
  }, [active, agentName, isStreaming, messages.length, tr, windowId]);

  const handleSend = useCallback(
    (text: string, attachments?: ChatAttachment[]) => {
      if (!threadId) return;
      sendMessage(
        text,
        "",
        agentId,
        attachments,
        undefined,
        activeThread?.model_ref ?? undefined,
        undefined,
        undefined,
        undefined,
        undefined,
        activeThread?.reasoning_mode ?? "auto",
        activeThread?.reasoning_effort ?? null,
        conversationMode,
        hitlPolicy,
        "web",
      );
    },
    [
      activeThread,
      agentId,
      conversationMode,
      hitlPolicy,
      sendMessage,
      threadId,
    ],
  );

  const handleNewChat = useCallback(() => {
    setPanel(null);
    void createThread();
  }, [createThread]);

  const handleEditUserMessage = useCallback(
    (messageId: string, newText: string) => {
      if (!threadId) return;
      editAndResend(messageId, newText, threadId, agentId);
    },
    [agentId, editAndResend, threadId],
  );

  const handleHitlDecision: HitlDecisionHandler = useCallback(
    (decisions, policy, pendingId) => {
      resumeHitl(decisions, undefined, undefined, policy, pendingId);
    },
    [resumeHitl],
  );

  const handleSelectThread = useCallback(
    (id: string) => {
      setThreadId(id);
      rememberThread(agentId, id);
      setPanel(null);
    },
    [agentId],
  );

  const handleAction = useCallback(
    (action: AppActionDef) => {
      if (!activeApp) return;
      prefill(actionPromptFor(action, activeApp, tr));
    },
    [activeApp, prefill, tr],
  );

  const quickPrompts = useMemo(() => {
    if (activeApp) {
      return APP_ACTIONS.map((action) => ({
        key: action.key,
        icon: action.icon,
        label: tr(action.labelKey, action.label),
        prompt: actionPromptFor(action, activeApp, tr),
      }));
    }
    return [
      {
        key: "apps",
        icon: AppWindow,
        label: tr("apps.desktop.quickApps", "Review my MiniApps"),
        prompt: tr(
          "apps.desktop.quickAppsPrompt",
          "Please list the MiniApps I own, then briefly report each one's status and the most useful next improvement.",
        ),
      },
      {
        key: "plan",
        icon: MessageSquare,
        label: tr("apps.desktop.quickPlan", "Suggest next steps"),
        prompt: tr(
          "apps.desktop.quickPlanPrompt",
          "Based on my recent MiniApps and conversations, give me three concrete next steps I can take.",
        ),
      },
    ];
  }, [activeApp, tr]);

  if (!threadId && threadsLoading) {
    return (
      <div className={styles.desktopChatBoot} data-testid="desktop-chat">
        <Spin />
      </div>
    );
  }

  if (!threadId && threadsError) {
    return (
      <div className={styles.desktopChatBoot} data-testid="desktop-chat">
        <span>
          {tr(
            "apps.desktop.chatBootFailed",
            "Could not load this expert's conversations.",
          )}
        </span>
        <button
          type="button"
          className={styles.desktopChatBootRetry}
          onClick={() => setBootKey((key) => key + 1)}
        >
          <RefreshCw size={13} />
          {tr("common.retry", "Retry")}
        </button>
      </div>
    );
  }

  return (
    <div className={styles.desktopChat} data-testid="desktop-chat">
      <header className={styles.desktopChatHeader}>
        <span
          className={styles.desktopChatAvatar}
          style={{ color: agent?.color || "#6366f1" }}
        >
          <ExpertIcon
            iconName={agent?.icon_name}
            iconUrl={agent?.icon_url}
            size={20}
          />
        </span>
        <div className={styles.desktopChatIdentity}>
          <span className={styles.desktopChatName}>{agentName}</span>
          <span className={styles.desktopChatState} data-state={stateKey}>
            <span className={styles.desktopChatStateDot} />
            {statusLabel}
          </span>
        </div>
        <div className={styles.desktopChatHeaderActions}>
          <button
            type="button"
            className={styles.desktopChatThreadButton}
            data-testid="desktop-chat-thread-trigger"
            aria-haspopup="dialog"
            aria-expanded={panel === "threads"}
            onClick={() =>
              setPanel((current) => (current === "threads" ? null : "threads"))
            }
          >
            <MessageSquare size={13} />
            <span>{title}</span>
            <ChevronDown size={12} />
          </button>
          <Tooltip title={tr("apps.desktop.newChat", "New conversation")}>
            <button
              type="button"
              className={styles.desktopChatIconButton}
              data-testid="desktop-chat-new"
              aria-label={tr("apps.desktop.newChat", "New conversation")}
              onClick={handleNewChat}
            >
              <Plus size={15} />
            </button>
          </Tooltip>
        </div>
      </header>

      {panel && (
        <>
          <button
            type="button"
            className={styles.desktopChatPanelBackdrop}
            aria-label={tr("apps.desktop.closePanel", "Close panel")}
            onClick={() => setPanel(null)}
          />
          <div
            className={styles.desktopChatPanel}
            role="dialog"
            aria-label={
              panel === "threads"
                ? tr("apps.desktop.sessions", "Conversations")
                : tr("apps.desktop.pickApp", "Choose a MiniApp")
            }
            data-testid={`desktop-chat-panel-${panel}`}
          >
            {panel === "threads" ? (
              <>
                <div className={styles.desktopChatPanelHeader}>
                  <span>{tr("apps.desktop.sessions", "Conversations")}</span>
                  <button
                    type="button"
                    className={styles.desktopChatPanelAction}
                    onClick={handleNewChat}
                  >
                    <Plus size={12} />
                    {tr("apps.desktop.newChat", "New conversation")}
                  </button>
                </div>
                <div className={styles.desktopChatPanelList}>
                  {threads.length === 0 ? (
                    <div className={styles.desktopChatPanelEmpty}>
                      {tr("apps.desktop.noSessions", "No conversations yet.")}
                    </div>
                  ) : (
                    threads.map((thread) => {
                      const active = thread.thread_id === threadId;
                      const stamp = thread.last_active
                        ? formatMessageTime(thread.last_active * 1000, timeZone)
                        : "";
                      return (
                        <button
                          key={thread.thread_id}
                          type="button"
                          className={[
                            styles.desktopChatThreadItem,
                            active ? styles.desktopChatThreadItemActive : "",
                          ]
                            .filter(Boolean)
                            .join(" ")}
                          data-testid="desktop-chat-thread"
                          data-thread-id={thread.thread_id}
                          aria-pressed={active}
                          onClick={() => handleSelectThread(thread.thread_id)}
                        >
                          <span className={styles.desktopChatThreadItemMain}>
                            <span className={styles.desktopChatThreadItemName}>
                              {threadTitle(
                                thread,
                                tr(
                                  "apps.desktop.untitledThread",
                                  "New conversation",
                                ),
                              )}
                            </span>
                            <span className={styles.desktopChatThreadItemMeta}>
                              {stamp || tr("apps.desktop.emptyThread", "Empty")}
                              {thread.channel_type !== "dashboard"
                                ? ` · ${thread.channel_type}`
                                : ""}
                            </span>
                          </span>
                          {active && <Check size={14} />}
                        </button>
                      );
                    })
                  )}
                </div>
              </>
            ) : (
              <>
                <div className={styles.desktopChatPanelHeader}>
                  <span>{tr("apps.desktop.pickApp", "Choose a MiniApp")}</span>
                </div>
                <div className={styles.desktopChatPanelList}>
                  {apps.length === 0 ? (
                    <div className={styles.desktopChatPanelEmpty}>
                      {tr(
                        "apps.desktop.noAppsForExpert",
                        "This expert has not created a MiniApp yet.",
                      )}
                    </div>
                  ) : (
                    apps.map((app) => {
                      const active = app.app_id === activeAppId;
                      return (
                        <div
                          key={app.app_id}
                          className={styles.desktopChatAppRow}
                        >
                          <button
                            type="button"
                            className={[
                              styles.desktopChatAppItem,
                              active ? styles.desktopChatAppItemActive : "",
                            ]
                              .filter(Boolean)
                              .join(" ")}
                            data-testid="desktop-chat-app-option"
                            data-app-id={app.app_id}
                            aria-pressed={active}
                            onClick={() => {
                              setActiveAppId(app.app_id);
                              setPanel(null);
                            }}
                          >
                            <span className={styles.desktopChatAppItemIcon}>
                              <AppWindow size={14} />
                            </span>
                            <span className={styles.desktopChatAppItemText}>
                              <span className={styles.desktopChatAppItemName}>
                                {app.name}
                              </span>
                              <span className={styles.desktopChatAppItemMeta}>
                                {fillTemplate(
                                  tr("apps.desktop.appRows", "{{count}} rows"),
                                  { count: app.row_count },
                                )}
                              </span>
                            </span>
                            {active && <Check size={14} />}
                          </button>
                          {onOpenApp && (
                            <button
                              type="button"
                              className={styles.desktopChatAppOpen}
                              data-testid="desktop-chat-app-open"
                              aria-label={`${tr(
                                "apps.desktop.openMiniApp",
                                "Open",
                              )} ${app.name}`}
                              title={tr("apps.desktop.openMiniApp", "Open")}
                              onClick={() => {
                                setActiveAppId(app.app_id);
                                setPanel(null);
                                onOpenApp(app);
                              }}
                            >
                              <ArrowUpRight size={14} />
                            </button>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
                {activeApp && (
                  <button
                    type="button"
                    className={styles.desktopChatPanelClear}
                    onClick={() => {
                      setActiveAppId(null);
                      setPanel(null);
                    }}
                  >
                    <X size={12} />
                    {tr(
                      "apps.desktop.clearAppContext",
                      "Clear MiniApp context",
                    )}
                  </button>
                )}
              </>
            )}
          </div>
        </>
      )}

      {(activeApp || apps.length > 0) && (
        <div
          className={styles.desktopChatContext}
          data-testid="desktop-chat-context"
        >
          {activeApp ? (
            <>
              <span className={styles.desktopChatContextIcon}>
                <AppWindow size={13} />
              </span>
              <span className={styles.desktopChatContextName}>
                {activeApp.name}
              </span>
              <div className={styles.desktopChatContextActions}>
                {onOpenApp && (
                  <button
                    type="button"
                    className={styles.desktopChatContextOpen}
                    data-testid="desktop-chat-open-app"
                    title={tr("apps.desktop.openMiniApp", "Open")}
                    onClick={() => onOpenApp(activeApp)}
                  >
                    <ArrowUpRight size={12} />
                    <span>{tr("apps.desktop.openMiniApp", "Open")}</span>
                  </button>
                )}
                {APP_ACTIONS.map(({ key, icon: Icon, labelKey, label }) => (
                  <button
                    key={key}
                    type="button"
                    className={styles.desktopChatContextAction}
                    data-testid={`desktop-chat-action-${key}`}
                    onClick={() => {
                      const action = APP_ACTIONS.find(
                        (item) => item.key === key,
                      );
                      if (action) handleAction(action);
                    }}
                  >
                    <Icon size={12} />
                    <span>{tr(labelKey, label)}</span>
                  </button>
                ))}
              </div>
              <button
                type="button"
                className={styles.desktopChatContextClear}
                aria-label={tr(
                  "apps.desktop.clearAppContext",
                  "Clear MiniApp context",
                )}
                onClick={() => setActiveAppId(null)}
              >
                <X size={12} />
              </button>
            </>
          ) : (
            <button
              type="button"
              className={styles.desktopChatContextPick}
              data-testid="desktop-chat-pick-app"
              onClick={() => setPanel("apps")}
            >
              <AppWindow size={13} />
              {tr("apps.desktop.pickApp", "Choose a MiniApp")}
              <span className={styles.desktopChatContextCount}>
                {apps.length}
              </span>
              <ChevronDown size={12} />
            </button>
          )}
        </div>
      )}

      <div className={styles.desktopChatBody}>
        {historyError && (
          <div className={styles.desktopChatError}>
            <span>
              {tr(
                "apps.desktop.historyError",
                "Could not load this conversation.",
              )}
            </span>
            <button type="button" onClick={() => void retryHistory()}>
              <RefreshCw size={12} />
              {tr("common.retry", "Retry")}
            </button>
          </div>
        )}
        {!historyLoading && historyHydrated && messages.length === 0 ? (
          <div className={styles.desktopChatWelcome}>
            <span
              className={styles.desktopChatWelcomeAvatar}
              style={{ color: agent?.color || "#6366f1" }}
            >
              <ExpertIcon
                iconName={agent?.icon_name}
                iconUrl={agent?.icon_url}
                size={30}
              />
            </span>
            <h3>{agentName}</h3>
            <p>
              {agent?.welcome_message ||
                agent?.description ||
                tr(
                  "apps.desktop.chatWelcome",
                  "Ask about your MiniApps, data, or anything else on this desktop.",
                )}
            </p>
            <div className={styles.desktopChatQuickActions}>
              {quickPrompts.map(({ key, icon: Icon, label, prompt }) => (
                <button
                  key={key}
                  type="button"
                  className={styles.desktopChatQuickAction}
                  onClick={() => prefill(prompt)}
                >
                  <Icon size={14} />
                  <span>{label}</span>
                </button>
              ))}
            </div>
            {!activeApp && apps.length > 0 && (
              <button
                type="button"
                className={styles.desktopChatWelcomePick}
                onClick={() => setPanel("apps")}
              >
                <AppWindow size={13} />
                {tr(
                  "apps.desktop.pickAppHint",
                  "Pick a MiniApp to work on it together",
                )}
              </button>
            )}
          </div>
        ) : (
          <MessageList
            messages={messages}
            agentId={agentId}
            loading={historyLoading}
            historyHasMore={historyHasMore}
            historyLoadingMore={historyLoadingMore}
            historyRefreshing={historyRefreshing}
            onLoadMoreHistory={loadMoreHistory}
            onRefreshHistory={refreshHistory}
            isStreaming={isStreaming}
            liveSpeakers={liveSpeakers}
            thinkingStartedAt={thinkingStartedAt}
            sessionKey={threadId ?? undefined}
            onEditUserMessage={handleEditUserMessage}
            onHitlDecision={handleHitlDecision}
            compactProcess
          />
        )}
      </div>

      <div className={styles.desktopChatComposer}>
        {!chatReady && (
          <div className={styles.desktopChatOffline}>
            {tr(
              "apps.desktop.expertOffline",
              "This expert is not running. Start it before sending a message.",
            )}
          </div>
        )}
        {threadId && (
          <ChatInput
            ref={inputRef}
            onSend={handleSend}
            onCancel={cancelStream}
            onNewChat={handleNewChat}
            isStreaming={isStreaming}
            disabled={!chatReady}
            agentId={agentId}
            threadId={threadId}
            defaultModel={agent?.default_model ?? null}
            conversationMode={conversationMode}
            hitlPolicy={hitlPolicy ?? undefined}
            availableAgents={agents}
            contextUsedTokens={contextUsage?.input_tokens ?? null}
            contextMaxTokens={agent?.max_input_length ?? 128_000}
            decisionSaved={contextUsage?.decision_saved ?? 0}
          />
        )}
      </div>

      <footer className={styles.desktopChatFooter}>
        <span className={styles.desktopChatFooterState} data-state={stateKey}>
          <span className={styles.desktopChatFooterDot} />
          {statusLabel}
        </span>
        <span>
          {fillTemplate(tr("apps.desktop.messageCount", "{{count}} messages"), {
            count: messages.length,
          })}
        </span>
        {runUsage?.total_tokens ? (
          <span>{runUsage.total_tokens.toLocaleString()} tokens</span>
        ) : null}
        <span className={styles.desktopChatFooterSpacer} />
        <span className={styles.desktopChatFooterModel}>
          {agent?.default_model ||
            tr("apps.desktop.defaultModel", "Default model")}
        </span>
      </footer>
    </div>
  );
}
