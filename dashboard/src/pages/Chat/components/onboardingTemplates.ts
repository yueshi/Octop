/**
 * Recommended expert templates for the "no agents yet" onboarding cards.
 *
 * Ids reference `src/octop/infra/agents/experts/library/`. A missing id is
 * skipped by the caller, so this list may drift without breaking the page.
 */
const COMMON_TEMPLATES = [
  "general-assistant",
  "office-automation",
  "productivity",
] as const;

const ADMIN_TEMPLATES = [
  "general-assistant",
  "business-operations",
  "office-automation",
] as const;

export function recommendedTemplateIds(
  role?: string | null,
): readonly string[] {
  return role === "admin" ? ADMIN_TEMPLATES : COMMON_TEMPLATES;
}

/** Ordered catalog entries that exist and are on the recommended list. */
export function pickRecommended<T extends { id: string }>(
  catalog: readonly T[],
  role?: string | null,
): T[] {
  const byId = new Map(catalog.map((item) => [item.id, item]));
  const out: T[] = [];
  for (const id of recommendedTemplateIds(role)) {
    const found = byId.get(id);
    if (found) out.push(found);
  }
  return out;
}

/** Append " 2", " 3" when the template label is already taken. */
export function numberedName(base: string, attempt: number): string {
  return attempt <= 1 ? base : `${base} ${attempt}`;
}
