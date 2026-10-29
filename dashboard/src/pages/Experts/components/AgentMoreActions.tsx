import { Dropdown, Tooltip } from "antd";
import type { MenuProps } from "antd";
import {
  Bot,
  Brain,
  MoreHorizontal,
  Notebook,
  Puzzle,
  Sparkles,
  Target,
  Waypoints,
  Wrench,
} from "lucide-react";
import { useTranslation } from "react-i18next";

interface AgentMoreActionsProps {
  buttonClassName: string;
  onSkills: () => void;
  onSubagents: () => void;
  onTools: () => void;
  onPlugins: () => void;
  onMbti: () => void;
  onMemory: () => void;
  onChannels: () => void;
  onGoals?: () => void;
  onBoard?: () => void;
  autonomyEnabled?: boolean;
  onToggleAutonomy?: () => void;
}

/** Overflow menu for expert catalog actions (skills / subagents / …). */
export default function AgentMoreActions({
  buttonClassName,
  onSkills,
  onSubagents,
  onTools,
  onPlugins,
  onMbti,
  onMemory,
  onChannels,
  onGoals,
  onBoard,
  autonomyEnabled,
  onToggleAutonomy,
}: AgentMoreActionsProps) {
  const { t } = useTranslation();

  const items: MenuProps["items"] = [
    ...(onGoals
      ? [
          {
            key: "goals",
            icon: <Target size={14} />,
            label: t("goals.title"),
            onClick: onGoals,
          },
        ]
      : []),
    ...(onToggleAutonomy
      ? [
          {
            key: "autonomy",
            icon: <Bot size={14} />,
            label: t(
              autonomyEnabled
                ? "agents.autonomy.dismiss"
                : "agents.autonomy.hire",
            ),
            onClick: onToggleAutonomy,
          },
        ]
      : []),
    ...(onBoard
      ? [
          {
            key: "board",
            icon: <Target size={14} />,
            label: t("agents.autonomy.board"),
            onClick: onBoard,
          },
        ]
      : []),
    {
      key: "skills",
      icon: <Sparkles size={14} />,
      label: t("experts.skillsBtn"),
      onClick: onSkills,
    },
    {
      key: "subagents",
      icon: <Bot size={14} />,
      label: t("experts.subagentsBtn"),
      onClick: onSubagents,
    },
    {
      key: "tools",
      icon: <Wrench size={14} />,
      label: t("experts.toolsBtn"),
      onClick: onTools,
    },
    {
      key: "plugins",
      icon: <Puzzle size={14} />,
      label: t("experts.pluginsBtn"),
      onClick: onPlugins,
    },
    {
      key: "mbti",
      icon: <Brain size={14} />,
      label: t("experts.mbtiBtn"),
      onClick: onMbti,
    },
    {
      key: "memory",
      icon: <Notebook size={14} />,
      label: t("experts.memoryBtn"),
      onClick: onMemory,
    },
    {
      key: "channels",
      icon: <Waypoints size={14} />,
      label: t("experts.channelsBtn"),
      onClick: onChannels,
    },
  ];

  return (
    <Dropdown menu={{ items }} trigger={["click"]} placement="bottomRight">
      <Tooltip title={t("common.more")} mouseEnterDelay={0.5}>
        <button
          type="button"
          className={buttonClassName}
          aria-label={t("common.more")}
        >
          <MoreHorizontal size={13} />
        </button>
      </Tooltip>
    </Dropdown>
  );
}
