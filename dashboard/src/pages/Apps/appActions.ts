import {
  Database,
  Gauge,
  Sparkles,
  Wrench,
  type LucideIcon,
} from "lucide-react";

import type { AppSummary } from "../../api/modules/apps";

export type AppActionKey = "optimize" | "status" | "data" | "config";

export interface AppActionDef {
  key: AppActionKey;
  icon: LucideIcon;
  labelKey: string;
  label: string;
  promptKey: string;
  prompt: string;
}

/** Expert-backed actions on one MiniApp. Prompts are filled into the composer. */
export const APP_ACTIONS: readonly AppActionDef[] = [
  {
    key: "optimize",
    icon: Sparkles,
    labelKey: "apps.desktop.optimize",
    label: "Optimize",
    promptKey: "apps.desktop.optimizePrompt",
    prompt:
      "Please optimize the UI and interactions of the MiniApp “{{name}}” (app_id: {{app_id}}). Inspect its HTML, tables, and data first, identify the three highest-impact problems, and fix them directly. When done, open the result with app_open and summarize what changed.",
  },
  {
    key: "status",
    icon: Gauge,
    labelKey: "apps.desktop.status",
    label: "Status",
    promptKey: "apps.desktop.statusPrompt",
    prompt:
      "Please report the status of the MiniApp “{{name}}” (app_id: {{app_id}}): list its tables and row counts, last update time, HTML size, publication/share status, and page capabilities. Keep it brief.",
  },
  {
    key: "data",
    icon: Database,
    labelKey: "apps.desktop.data",
    label: "Data",
    promptKey: "apps.desktop.dataPrompt",
    prompt:
      "Please help manage the data of the MiniApp “{{name}}” (app_id: {{app_id}}). Read the schema, row counts, and sample rows of each table, point out data-quality issues, and propose a cleanup plan. List the plan before adding, updating, or deleting rows.",
  },
  {
    key: "config",
    icon: Wrench,
    labelKey: "apps.desktop.config",
    label: "Config",
    promptKey: "apps.desktop.configPrompt",
    prompt:
      "Please review and adjust the configuration of the MiniApp “{{name}}” (app_id: {{app_id}}): check its table schema, page capabilities, and public visibility, and recommend least-privilege settings. Apply changes directly and explain the result.",
  },
];

export function fillTemplate(
  template: string,
  values: Record<string, string | number>,
): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_match, key: string) =>
    String(values[key] ?? ""),
  );
}

/** Fill one action prompt for an app. */
export function actionPromptFor(
  action: AppActionDef,
  app: Pick<AppSummary, "name" | "app_id">,
  t: (key: string, fallback: string) => string,
): string {
  return fillTemplate(t(action.promptKey, action.prompt), {
    name: app.name,
    app_id: app.app_id,
  });
}
