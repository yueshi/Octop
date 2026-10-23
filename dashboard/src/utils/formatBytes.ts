/**
 * Human-readable byte size for the light-app file cards.
 *
 * Byte counts come from the workspace backend, so they can be missing
 * (`null`) or zero; callers get a dash instead of a misleading "0 B".
 */

const UNITS = ["B", "KB", "MB", "GB"];

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) {
    return "—";
  }
  if (bytes <= 0) return "0 B";
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = unit === 0 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${UNITS[unit]}`;
}
