import { useCallback, useEffect, useState } from "react";
import { Button, Empty, Spin, Typography } from "antd";
import { ArrowLeft, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router-dom";

import { appsApi, type AppSummary } from "../../api/modules/apps";
import { boardsApi, type BoardDetail } from "../../api/modules/boards";
import BoardGrid from "./BoardGrid";
import { apiErrorMessage } from "../../utils/apiError";
import styles from "./index.module.less";

const { Text } = Typography;

/** Standalone board presentation, opened in its own browser tab. */
export default function BoardViewPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { boardId = "" } = useParams();
  const [board, setBoard] = useState<BoardDetail | null>(null);
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!boardId) return;
    setLoading(true);
    setError("");
    try {
      const [detail, appList] = await Promise.all([
        boardsApi.get(boardId),
        appsApi.list(),
      ]);
      setBoard(detail);
      setApps(appList.apps);
    } catch (err) {
      setError(
        apiErrorMessage(
          err,
          t("apps.board.loadFailed", "Could not load this board"),
          t,
        ),
      );
    } finally {
      setLoading(false);
    }
  }, [boardId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const openApp = useCallback(
    (appId: string) => navigate(`/apps/${appId}`),
    [navigate],
  );

  const composeMessage = useCallback(
    (agentId: string, text: string) => {
      if (!text.trim()) return;
      navigate(`/chat/${encodeURIComponent(agentId)}`, {
        state: { prefillInput: text },
      });
    },
    [navigate],
  );

  return (
    <div className={styles.presentRoot}>
      <div className={styles.presentBar}>
        <Button
          type="text"
          icon={<ArrowLeft size={16} />}
          onClick={() => navigate("/apps")}
        >
          {t("apps.board.back", "Back to workbench")}
        </Button>
        <Text ellipsis className={styles.presentTitle}>
          {board?.name ?? t("apps.board.present", "Present")}
        </Text>
        <Button
          type="text"
          icon={<RefreshCw size={16} />}
          aria-label={t("apps.board.refresh", "Refresh")}
          onClick={() => void load()}
        />
      </div>
      <div className={styles.presentBody}>
        {loading ? (
          <div style={{ padding: 48, textAlign: "center" }}>
            <Spin />
          </div>
        ) : error || !board ? (
          <Empty
            description={error || t("apps.board.none", "No board yet.")}
            style={{ padding: "64px 0" }}
          >
            <Button type="primary" onClick={() => navigate("/apps")}>
              {t("apps.board.back", "Back to workbench")}
            </Button>
          </Empty>
        ) : (
          <BoardGrid
            items={board.layout.items}
            apps={apps}
            onOpenApp={openApp}
            onComposeMessage={composeMessage}
          />
        )}
      </div>
    </div>
  );
}
