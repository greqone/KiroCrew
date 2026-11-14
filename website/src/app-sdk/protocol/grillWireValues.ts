// WIRE VALUES ONLY for the `[GRILL:]` discussion card (grillMarker.ts): the
// marker opener the conductor writes, and the exact replies the card sends back.
// The conductor matches these by value (worker-grill plan, data contract 1), so
// they are fixed in every locale; translating one would break the hand-off
// rather than localize it. The card's own labels live in the i18n catalog.

/** Opens a marker line. */
export const GRILL_OPEN = '[GRILL:'

/** The text the Start button sends. */
export function grillStartText(p: { q: string; must: string; skip: string }): string {
  return `开始讨论\n问题：${p.q}\n必须：${p.must}\n不讨论：${p.skip}`
}

/** The text the decline button sends. */
export const GRILL_DECLINE_TEXT = '不用讨论了'
