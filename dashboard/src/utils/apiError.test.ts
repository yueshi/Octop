import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";

import { apiErrorMessage, parseApiError } from "./apiError";

const TEMPLATES: Record<string, string> = {
  "apiErrors.APP_ROW_INVALID": "Invalid row.",
  "apiErrors.CONNECTOR_MCP_URL_INVALID": "Invalid MCP server URL: {{reason}}",
};

const t = ((key: string, options?: Record<string, unknown>) => {
  const template = TEMPLATES[key];
  if (!template) return key;
  return template.replace("{{reason}}", String(options?.reason ?? ""));
}) as unknown as TFunction;

function apiError(payload: object): Error {
  return new Error(`API error 400: ${JSON.stringify(payload)}`);
}

describe("apiErrorMessage", () => {
  it("does not repeat a reason the localized template already contains", () => {
    const error = apiError({
      error: {
        code: "CONNECTOR_MCP_URL_INVALID",
        details: { reason: "url is required" },
      },
    });
    expect(apiErrorMessage(error, "fallback", t)).toBe(
      "Invalid MCP server URL: url is required",
    );
  });

  it("uses the localized message for a known code", () => {
    const error = apiError({
      error: { code: "APP_ROW_INVALID", message: "Invalid row." },
    });
    expect(apiErrorMessage(error, "fallback", t)).toBe("Invalid row.");
  });

  it("falls back to the server message for unknown codes", () => {
    const error = apiError({
      error: { code: "SOMETHING_NEW", message: "Server detail" },
    });
    expect(apiErrorMessage(error, "fallback", t)).toBe("Server detail");
  });

  it("parses the error envelope", () => {
    const error = apiError({
      error: { code: "APP_NOT_FOUND", message: "gone" },
    });
    expect(parseApiError(error)).toMatchObject({
      code: "APP_NOT_FOUND",
      message: "gone",
    });
  });
});
