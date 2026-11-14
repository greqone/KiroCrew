/**
 * The discussion card a `[GRILL: {json}]` marker draws inside a reply: the
 * conductor proposes that workers grill a question among themselves, and the
 * user edits its direction (question, must, not discussing) before starting it.
 *
 * It never holds the turn: the card only composes the next message. Start and
 * No thanks hand their text, with the reply's chat, to the chat page
 * (`mc:compose-send`), which sends it when Quick Send is on and otherwise puts
 * it in the chat box, the same rule as a follow-up chip. Once used, the card
 * locks for as long as this page shows it, and says what happened: sent, or put
 * in the chat box. A send that then fails shows in the chat's own error row,
 * as a chip's does. A reload opens the card again; the conductor ignores a
 * repeated start.
 */
import { useId, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { GRILL_DECLINE_TEXT, GRILL_LIMITS, grillStartText, type GrillProposal } from '../../app-sdk/protocol/grillMarker'
import { btnCls, btnPrimaryCls } from '../redactionPrimitives'
import ErrorNotice from '../ErrorNotice'

/** The event the card dispatches. The host serving `slot` sets `result`: `sent`
 *  when the text went out, `staged` when it only reached the chat box. */
export const COMPOSE_SEND_EVENT = 'mc:compose-send'
export type ComposeSendResult = 'sent' | 'staged'
export interface ComposeSendDetail { text: string; slot: string; result: ComposeSendResult | null }

/** Hand `text` to the page serving `slot`; null when none took it (nothing happened). */
function composeSend(text: string, slot: string | undefined): ComposeSendResult | null {
  if (!slot) return null
  const detail: ComposeSendDetail = { text, slot, result: null }
  window.dispatchEvent(new CustomEvent(COMPOSE_SEND_EVENT, { detail }))
  return detail.result
}

const FIELD = 'w-full min-w-0 rounded-md border border-border bg-bg px-2 py-1 text-[13px] leading-5 text-text focus:outline-none focus-visible:border-accent focus-visible:ring-1 focus-visible:ring-accent disabled:opacity-70'
const LABEL = 'text-[11px] leading-4 font-semibold text-muted'

/** `slotKey`: the chat this reply belongs to. */
export default function GrillCard({ grill, slotKey }: { grill: GrillProposal; slotKey?: string }) {
  const { t } = useTranslation()
  const id = useId()
  const [q, setQ] = useState(grill.q)
  const [must, setMust] = useState(grill.must)
  const [skip, setSkip] = useState(grill.skip)
  const [result, setResult] = useState<ComposeSendResult | null>(null)
  const [unhandled, setUnhandled] = useState(false)
  const locked = result !== null

  const act = (kind: 'start' | 'decline') => {
    if (locked) return
    const text = kind === 'start' ? grillStartText({ q: q.trim(), must: must.trim(), skip: skip.trim() }) : GRILL_DECLINE_TEXT
    // shortcut: the chat page and ChatPane (split cells, member threads) take the
    // text; any other host (an SDK embed) leaves the card open and saying so.
    // Wire that host's composer to the event when one needs the card.
    const got = composeSend(text, slotKey)
    if (!got) { setUnhandled(true); return }
    setUnhandled(false)
    setResult(got)
  }

  const fields: { key: string; label: string; value: string; set: (v: string) => void }[] = [
    { key: 'q', label: t('components.grillCard.question'), value: q, set: setQ },
    { key: 'must', label: t('components.grillCard.must'), value: must, set: setMust },
    { key: 'skip', label: t('components.grillCard.skip'), value: skip, set: setSkip },
  ]

  return (
    <div
      role="group"
      aria-label={t('components.grillCard.label')}
      data-testid="grill-card"
      data-result={result ?? undefined}
      className="mt-2 min-w-0 flex flex-col gap-2 rounded-lg border border-border bg-card text-card-fg px-3 py-2.5"
    >
      <div className="text-[13px] leading-5 font-semibold text-text-strong">{t('components.grillCard.label')}</div>
      {fields.map(f => (
        <div key={f.key} className="flex flex-col gap-0.5">
          <label htmlFor={`${id}-${f.key}`} className={LABEL}>{f.label}</label>
          <input
            id={`${id}-${f.key}`}
            aria-label={f.label}
            type="text"
            className={FIELD}
            value={f.value}
            maxLength={GRILL_LIMITS.text}
            disabled={locked}
            data-testid={`grill-card-${f.key}`}
            onChange={e => f.set(e.target.value)}
          />
        </div>
      ))}
      <div className="flex flex-col gap-0.5">
        <div className={LABEL}>{t('components.grillCard.who')}</div>
        <ul className="m-0 pl-4 list-disc text-[13px] leading-5 break-words" data-testid="grill-card-who">
          {grill.who.map((p, i) => <li key={i}><span className="font-semibold">{p.name}</span>: {p.stance}</li>)}
        </ul>
      </div>
      <div className="text-[12px] leading-4 text-muted" data-testid="grill-card-minutes">{t('components.grillCard.minutes', { count: grill.minutes })}</div>
      <div className="flex flex-wrap items-center gap-2">
        {locked ? (
          <span className="text-[12px] leading-5 text-muted" role="status" data-testid="grill-card-sent">
            {result === 'staged' ? t('components.grillCard.staged') : t('components.grillCard.sent')}
          </span>
        ) : (
          <>
            <button type="button" className={btnPrimaryCls} data-testid="grill-card-start" disabled={!q.trim()} onClick={() => act('start')}>
              {t('components.grillCard.start')}
            </button>
            <button type="button" className={btnCls} data-testid="grill-card-decline" onClick={() => act('decline')}>
              {t('components.grillCard.decline')}
            </button>
          </>
        )}
      </div>
      {/* No hand-off: asking the agent would navigate away from the card's unsent edited lines. */}
      {unhandled && <ErrorNotice message={t('components.grillCard.not_sent')} variant="inline" askAgent={false} onDismiss={() => setUnhandled(false)} testId="grill-card-unhandled" />}
    </div>
  )
}
