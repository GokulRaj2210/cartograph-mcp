/** Formatting helpers, the leaf module of the TS side. */

export const DEFAULT_LOCALE = "en-GB";

/** Pads a label to a fixed width. */
export function padLabel(label: string, width: number): string {
  return label.padEnd(width, " ");
}

/** Arrow-function export: must still register as a definition. */
export const slugify = (value: string): string =>
  value.toLowerCase().replace(/[^a-z0-9]+/g, "-");
