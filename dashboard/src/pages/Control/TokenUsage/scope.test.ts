import { describe, expect, it, vi } from "vitest";
import { fetchSummary, usageExportPath } from "./index";

const request = vi.hoisted(() => vi.fn());
vi.mock("../../../api/request", () => ({
  request: (...args: unknown[]) => request(...args),
  requestBlob: vi.fn(),
}));

describe("token usage scope", () => {
  it("keeps a non-admin on the caller-scoped endpoint", () => {
    // Non-admins pass null: never /admin, never a user_id.
    fetchSummary("last_30d", "by_day", "all", null);
    expect(request).toHaveBeenCalledWith(
      "/usage/summary?window=last_30d&granularity=by_day",
    );
  });

  it("scopes an admin to the selected user", () => {
    fetchSummary("last_30d", "by_day", "all", 7);
    expect(request).toHaveBeenLastCalledWith(
      "/admin/usage/summary?window=last_30d&granularity=by_day&user_id=7",
    );
  });

  it("omits user_id only when an admin explicitly asks for all users", () => {
    fetchSummary("last_30d", "by_day", "all", "all");
    expect(request).toHaveBeenLastCalledWith(
      "/admin/usage/summary?window=last_30d&granularity=by_day",
    );
  });

  it("mirrors the same scope in the Excel export path", () => {
    expect(usageExportPath("last_30d", "all", null)).toBe(
      "/usage/export.xlsx?window=last_30d",
    );
    expect(usageExportPath("last_30d", "all", 7)).toBe(
      "/admin/usage/export.xlsx?window=last_30d&user_id=7",
    );
    expect(usageExportPath("last_30d", "all", "all")).toBe(
      "/admin/usage/export.xlsx?window=last_30d",
    );
  });
});
