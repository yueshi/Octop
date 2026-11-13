import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Empty, Spin, Typography } from "antd";
import { ArrowLeft, Maximize2, RefreshCw } from "lucide-react";
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
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [board, setBoard] = useState<BoardDetail | null>(null);
  const [apps, setApps] = useState<AppSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [fullscreen, setFullscreen] = useState(false);

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

  // Keep the button in sync when fullscreen is left via Esc or the browser UI.
  useEffect(() => {
    const root = rootRef.current;
    const sync = () => setFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", sync);
    return () => {
      document.removeEventListener("fullscreenchange", sync);
      if (root && document.fullscreenElement === root) {
        void document.exitFullscreen().catch(() => {});
      }
    };
  }, []);

  // The toolbar is hidden in fullscreen; Esc / the browser exits it.
  const enterFullscreen = useCallback(() => {
    const el = rootRef.current;
    if (el) void el.requestFullscreen().catch(() => {});
  }, []);

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
    <div ref={rootRef} className={styles.presentRoot}>
      {!fullscreen && (
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
          <Button
            type="text"
            icon={<Maximize2 size={16} />}
            aria-label={t("apps.board.fullscreen", "Fullscreen")}
            onClick={enterFullscreen}
          />
        </div>
      )}
      <div className={styles.presentBody}>
        {loading ? (
          <div className={styles.boardState}>
            <Spin />
          </div>
        ) : error || !board ? (
          <div className={styles.boardState}>
            <Empty description={error || t("apps.board.none", "No board yet.")}>
              <Button type="primary" onClick={() => navigate("/apps")}>
                {t("apps.board.back", "Back to workbench")}
              </Button>
            </Empty>
          </div>
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
