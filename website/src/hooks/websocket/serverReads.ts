/** Unread from the gateway's read times, so every device agrees.
 *
 * Live frames badge only a window open when they arrive. Each slot row carries
 * `last_read_ts`, the newest read any device reported, and `read_epoch`, the
 * creation of the gateway's read store. After every slot list:
 *
 * - `clearCovered`: a badge a real read covers clears (read on another device,
 *   a read this window keeps in `readOutbox`, or a row it showed);
 * - `badgeMissed`: a slot whose newest agent row is newer than both the read
 *   and `read_epoch` badges (missed while closed).
 *
 * `read_epoch` is a floor, never a read: it proves nothing was read, so it never
 * clears a badge. A row without one (the gateway has not stored it yet) creates
 * no badge. A manual mark-as-unread is never cleared here.
 */
import type { QueryClient } from '@tanstack/react-query'
import { store, type AppDispatch } from '../../store'
import type { ChatSlot } from '../../types'
import { markSlotUnread, remoteSlotRead } from '../../store/dashboardSlice'
import { MANUAL_UNREAD, readCovers } from '../../store/dashboard/unread'
import { ackRead, queuedReads } from '../../lib/readOutbox'
import { newerTs, shownHere } from '../../lib/slotReadRelay'
import { appsMutingSessions, isSlotMuted } from '../sessionMute'
import { loadUnreadOnAttention } from '../unreadOnAttention'
import { isSlotOnScreen } from './attention'
import { slotLastTsOf } from '../../store/slotLastTs'

/** The time of the newest row that should badge *row* when nobody saw it arrive.
 *
 *  An agent row (`last_agent_ts`: assistant text or a tool call), the same rows
 *  the live rule badges. With "only when done or waiting" on, a running turn
 *  badges nothing yet unless it waits on the user (an approval or a question
 *  card), which the live rule badges too. A crewmate thread is left to its live
 *  rule, which also skips tool rows. */
export function unreadCandidateTs(row: ChatSlot, attentionOn: boolean): string | undefined {
  if (row.mode === 'member') return undefined
  if (attentionOn && row.running && !row.pending_approval && !row.needs_input) return undefined
  return row.last_agent_ts || undefined
}

const tsOf = (value: string | null | undefined): string | undefined =>
  typeof value === 'string' && value ? value : undefined

/** Clear *row*'s badge when a real read covers it; return that read, if any. */
export function clearCovered(row: ChatSlot, keptRead: string | undefined, dispatch: AppDispatch): string | undefined {
  const serverRead = tsOf(row.last_read_ts)
  if (keptRead !== undefined && serverRead !== undefined && readCovers(serverRead, keptRead)) {
    ackRead(row.key, serverRead)
    keptRead = undefined
  }
  const read = newerTs(newerTs(serverRead, keptRead), shownHere(row.key))
  if (read === undefined) return undefined
  const dashboard = store.getState().dashboard
  if (!dashboard.unreadSlots.includes(row.key)) return read
  const since = dashboard.unreadSince?.[row.key]
  if (since === MANUAL_UNREAD) return read
  const guard = since ?? slotLastTsOf(dashboard, row.key)
  if (guard && readCovers(read, guard)) dispatch(remoteSlotRead({ slot: row.key, readTs: read }))
  return read
}

/** Badge *row* when its newest agent row is newer than *read* and its `read_epoch`. */
export function badgeMissed(
  row: ChatSlot,
  read: string | undefined,
  { dispatch, attentionOn, mutedApps }: { dispatch: AppDispatch; attentionOn: boolean; mutedApps: ReturnType<typeof appsMutingSessions> },
): void {
  const epoch = tsOf(row.read_epoch)
  if (epoch === undefined) return
  const floor = newerTs(read, epoch)
  const candidate = unreadCandidateTs(row, attentionOn)
  if (!candidate || readCovers(floor, candidate)) return
  if (isSlotOnScreen(row.key) || isSlotMuted(store.getState().dashboard.slots, row.key, mutedApps)) return
  dispatch(markSlotUnread({ slot: row.key, ts: candidate }))
}

export interface ServerReadDeps {
  dispatch: AppDispatch
  queryClient: QueryClient
}

export function applyServerReads(rows: readonly ChatSlot[], { dispatch, queryClient }: ServerReadDeps): void {
  const kept = queuedReads()
  const attentionOn = loadUnreadOnAttention()
  const mutedApps = appsMutingSessions(queryClient.getQueryData(['kirocrewConfig']))
  for (const row of rows) {
    const read = clearCovered(row, kept[row.key], dispatch)
    badgeMissed(row, read, { dispatch, attentionOn, mutedApps })
  }
}
