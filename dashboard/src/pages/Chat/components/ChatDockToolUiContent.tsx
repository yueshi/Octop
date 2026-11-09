import { useTranslation } from "react-i18next";

import { parseOctopToolOutput } from "../../../plugins/toolRenderers/parseToolOutput";
import { useToolMessageByCallId } from "../hooks/useToolMessageByCallId";
import { ToolDetailsInline } from "./MessageBubble";
import styles from "../index.module.less";

interface ChatDockToolUiContentProps {
  threadId: string | null;
  callId: string;
  agentId: string;
  isStreamingTurn: boolean;
}

export default function ChatDockToolUiContent({
  threadId,
  callId,
  agentId,
  isStreamingTurn,
}: ChatDockToolUiContentProps) {
  const { t } = useTranslation();
  const message = useToolMessageByCallId(threadId, callId);

  if (!message?.toolData) {
    return (
      <div className={styles.dockToolUiMissing}>
        {t(
          "chat.dockToolUiMissing",
          "Tool result is no longer available in this conversation.",
        )}
      </div>
    );
  }

  const isStreaming = message.status === "streaming" && isStreamingTurn;
  const octopUi = parseOctopToolOutput(message.toolData.output).octopUi;
  const isAppCard = octopUi?.renderer === "app_card";

  return (
    <div
      className={`${styles.dockToolUiBody} ${
        isAppCard ? styles.dockToolUiBodyApp : ""
      }`}
    >
      <ToolDetailsInline
        toolData={message.toolData}
        isStreaming={isStreaming}
        agentId={agentId}
        forceInline
      />
    </div>
  );
}
