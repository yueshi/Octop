import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Result, Select, Spin } from "antd";
import { Settings } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { message } from "@/utils/antdMessage";
import { expertCatalogApi } from "../../../api/modules/expertCatalog";
import type { ExpertCatalogEntry } from "../../../api/modules/expertCatalog";
import { providerApi } from "../../../api/modules/provider";
import type { ResolvedModel } from "../../../api/types/provider";
import { OctopEmptyMascot } from "../../../components/EmptyState";
import type { OctopAgent } from "../../../context/AgentContext";
import { useCurrentUser } from "../../../hooks/useCurrentUser";
import expertStyles from "../../Experts/index.module.less";
import { ExpertCard } from "../../Experts/components/ExpertCard";
import type { ExpertSummary } from "../../Experts/components/ExpertCard";
import {
  formatAgentError,
  isAgentModelConfigError,
} from "../../../utils/agentError";
import { apiErrorMessage, parseApiError } from "../../../utils/apiError";
import { pickLocale } from "../../../utils/localizedText";
import styles from "../index.module.less";
import { numberedName, pickRecommended } from "./onboardingTemplates";

interface AgentNotReadyScreenProps {
  agent: OctopAgent | null;
  noAgents?: boolean;
  loading?: boolean;
}

type OnboardingPhase = "loading" | "error" | "no-model" | "ready";

function modelRef(model: ResolvedModel): string {
  return `${model.provider_name}/${model.model}`;
}

function isNameTaken(err: unknown): boolean {
  if (parseApiError(err)?.code === "AGENT_NAME_TAKEN") return true;
  const text = err instanceof Error ? err.message : String(err);
  return text.includes("AGENT_NAME_TAKEN");
}

function NoAgentsOnboarding() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const user = useCurrentUser();
  const lang: "zh" | "en" = i18n.language?.startsWith("zh") ? "zh" : "en";
  const isAdmin = user?.role === "admin";

  const [phase, setPhase] = useState<OnboardingPhase>("loading");
  const [models, setModels] = useState<ResolvedModel[]>([]);
  const [experts, setExperts] = useState<ExpertCatalogEntry[]>([]);
  const [modelIndex, setModelIndex] = useState(0);
  const [creatingId, setCreatingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setPhase("loading");
    try {
      const [modelList, catalog] = await Promise.all([
        providerApi.listResolvedModels(),
        expertCatalogApi.list(),
      ]);
      setModels(modelList);
      setExperts(pickRecommended(catalog, user?.role));
      setModelIndex(0);
      setPhase(modelList.length === 0 ? "no-model" : "ready");
    } catch {
      setPhase("error");
    }
  }, [user?.role]);

  useEffect(() => {
    void load();
  }, [load]);

  const modelOptions = useMemo(
    () =>
      models.map((item, index) => ({
        value: String(index),
        label: item.name || modelRef(item),
      })),
    [models],
  );

  const handleCreate = useCallback(
    async (expert: ExpertSummary) => {
      const model = models[modelIndex];
      if (!model) return;
      setCreatingId(expert.id);
      const base = pickLocale(expert.label, lang) || expert.id;
      try {
        for (let attempt = 1; attempt <= 3; attempt += 1) {
          try {
            const created = await expertCatalogApi.createFromExpert(expert.id, {
              name: numberedName(base, attempt),
              default_model: modelRef(model),
            });
            navigate(`/chat/${created.agent_id}`);
            return;
          } catch (err) {
            if (attempt === 3 || !isNameTaken(err)) throw err;
          }
        }
      } catch (err) {
        message.error(apiErrorMessage(err, t("experts.createFailed"), t));
      } finally {
        setCreatingId(null);
      }
    },
    [lang, modelIndex, models, navigate, t],
  );

  if (phase === "loading") {
    return (
      <div className={styles.noAgentsEmpty}>
        <div className={styles.noAgentsEmptyInner}>
          <Spin />
          <p className={styles.noAgentsEmptyHint}>
            {t("chat.noAgentsCheckModel")}
          </p>
        </div>
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className={styles.noAgentsEmpty}>
        <div className={styles.noAgentsEmptyInner}>
          <h1 className={styles.noAgentsEmptyTitle}>
            {t("chat.noAgentsModelFailed")}
          </h1>
          <p className={styles.noAgentsEmptyHint}>
            {t("chat.noAgentsModelFailedHint")}
          </p>
          <Button type="primary" size="large" onClick={() => void load()}>
            {t("chat.noAgentsRetry")}
          </Button>
        </div>
      </div>
    );
  }

  if (phase === "no-model") {
    return (
      <div className={styles.noAgentsEmpty}>
        <div className={styles.noAgentsEmptyInner}>
          <div className={styles.noAgentsEmptyIcon}>
            <OctopEmptyMascot className={styles.noAgentsEmptyMascot} />
          </div>
          <h1 className={styles.noAgentsEmptyTitle}>
            {isAdmin ? t("chat.noAgentsNoModelAdmin") : t("chat.noAgentsTitle")}
          </h1>
          <p className={styles.noAgentsEmptyHint}>
            {isAdmin
              ? t("chat.noAgentsNoModelAdminHint")
              : t("chat.noAgentsNoModelHint")}
          </p>
          {isAdmin ? (
            <Button
              type="primary"
              size="large"
              icon={<Settings size={14} />}
              onClick={() => navigate("/admin/models")}
            >
              {t("chat.noAgentsGoConfigModel")}
            </Button>
          ) : (
            <Button size="large" onClick={() => navigate("/experts")}>
              {t("chat.noAgentsContactAdmin")}
            </Button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={styles.noAgentsEmpty}>
      <div className={`${styles.noAgentsEmptyInner} ${styles.onboardingInner}`}>
        <div className={styles.noAgentsEmptyIcon}>
          <OctopEmptyMascot className={styles.noAgentsEmptyMascot} />
        </div>
        <h1 className={styles.noAgentsEmptyTitle}>{t("chat.noAgentsTitle")}</h1>
        <p className={styles.noAgentsEmptyHint}>{t("chat.noAgentsHint")}</p>
        <div className={expertStyles.cardGrid}>
          {experts.map((expert) => (
            <ExpertCard
              key={expert.id}
              expert={expert}
              lang={lang}
              isInstalled={false}
              disabled={creatingId !== null}
              onCreate={(item) => void handleCreate(item)}
            />
          ))}
        </div>
        {experts.length === 0 ? (
          <Button
            type="primary"
            size="large"
            onClick={() => navigate("/experts?tab=library")}
          >
            {t("chat.createExpert")}
          </Button>
        ) : (
          <>
            <div className={styles.onboardingModelRow}>
              <span>{t("chat.noAgentsModelLabel")}</span>
              <Select
                size="small"
                value={String(modelIndex)}
                options={modelOptions}
                onChange={(value) => setModelIndex(Number(value))}
                disabled={creatingId !== null}
                popupMatchSelectWidth={false}
              />
            </div>
            <Button
              type="link"
              onClick={() => navigate("/experts?tab=library")}
            >
              {t("chat.noAgentsViewAll")}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

export default function AgentNotReadyScreen({
  agent,
  noAgents = false,
  loading = false,
}: AgentNotReadyScreenProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  if (loading) {
    return (
      <div className={styles.agentNotReady}>
        <Spin />
      </div>
    );
  }

  if (noAgents) {
    return <NoAgentsOnboarding />;
  }

  if (!agent) {
    return (
      <div className={styles.agentNotReady}>
        <Result status="info" title={t("chat.pickAgent")} />
      </div>
    );
  }

  const state = agent.state;
  const errorText = formatAgentError(agent.last_error, t);
  const isModelError = isAgentModelConfigError(agent.last_error);
  const disconnected = Boolean(agent.bridge && agent.bridge_disconnected);

  let title = t("chat.agentNotRunning");
  let subTitle = t("chat.agentNotRunningHint");

  if (disconnected) {
    const inbound = Boolean(agent.bridge_inbound);
    title = t("chat.remoteExpert.disconnectedTitle");
    subTitle = inbound
      ? agent.bridge_connection_name
        ? t("chat.remoteExpert.waitForPeerNamed", {
            name: agent.bridge_connection_name,
          })
        : t("chat.remoteExpert.waitForPeer")
      : agent.bridge_connection_name
      ? t("chat.remoteExpert.disconnectedHintNamed", {
          name: agent.bridge_connection_name,
        })
      : t("chat.remoteExpert.disconnectedHint");
  } else if (state === "failed") {
    title = t("chat.agentFailed");
    subTitle = errorText || t("chat.agentFailedHint");
  } else if (state === "stopped" || state === "created") {
    title = t("chat.agentNotRunning");
    subTitle = t("chat.agentNotRunningHint");
  } else if (state === "starting" || state === "stopping") {
    title = t("chat.agentStarting");
    subTitle = t("chat.agentStartingHint");
  }

  return (
    <div className={styles.agentNotReady}>
      <Result
        status={
          disconnected
            ? "warning"
            : state === "failed"
            ? "error"
            : state === "stopped" || state === "created"
            ? "warning"
            : "info"
        }
        title={title}
        subTitle={subTitle}
        extra={
          disconnected ? (
            <Button type="primary" onClick={() => navigate("/bridge")}>
              {t(
                agent.bridge_inbound
                  ? "chat.remoteExpert.openBridge"
                  : "chat.remoteExpert.reconnect",
              )}
            </Button>
          ) : isModelError ? (
            <Button
              type="primary"
              icon={<Settings size={14} />}
              onClick={() => navigate("/admin/models")}
            >
              {t("modelConfig.configureButton")}
            </Button>
          ) : (
            <Button type="primary" onClick={() => navigate("/experts")}>
              {t("chat.goToExperts")}
            </Button>
          )
        }
      />
    </div>
  );
}
