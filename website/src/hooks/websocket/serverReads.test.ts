/**
 * Unread from the gateway's read times (`last_read_ts` and `read_epoch` on each slot row).
 *
 * The live rules badge a session only in a window that was open when its rows
 * arrived. These specs pin what every slot list now does on top of them: badge
 * what arrived while no window was open, clear what another device read, hold a
 * read the gateway has not confirmed yet so a laggy gateway cannot bring the
 * badge back, and never treat the store's epoch as a read.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { renderHook } from '@testing-library/react'
import { store } from '../../store'
import { sseSlots, markSlotRead, markSlotUnread, type SlotPatchFrame } from '../../store/dashboardSlice'
import { setActiveSlot } from '../../store/chatSlice'
import { MANUAL_UNREAD, __resetSharedUnreadQueueForTests, flushSharedUnread } from '../../store/dashboard/unread'
import type { ChatSlot } from '../../types'
import { applyServerReads } from './serverReads'
import { bindSlotReadSender, noteShownHere, noteUnsavedRowTs, _resetSlotReadRelayForTest } from '../../lib/slotReadRelay'
import { queueRead, queuedReads } from '../../lib/readOutbox'
import { UNREAD_ON_ATTENTION_KEY } from '../unreadOnAttention'
import { useSlotListSync } from './slotList'
import type { StreamBuffers } from './streamBuffers'

const EPOCH = '2026-11-13T08:00:00+00:00'
const READ = '2026-11-13T10:00:00+00:00'
const BEFORE = '2026-11-13T09:00:00+00:00'
const AFTER = '2026-11-13T11:00:00+00:00'

const row = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: key, messages: 1, running: false, last_read_ts: READ, read_epoch: EPOCH, last_agent_ts: AFTER, last_ts: AFTER, ...extra,
} as ChatSlot)

const queryClient = new QueryClient()
const sent: Array<[string, string | undefined]> = []

const apply = (rows: ChatSlot[]) => {
  store.dispatch(sseSlots(rows))
  applyServerReads(store.getState().dashboard.slots, { dispatch: store.dispatch, queryClient })
}
const buffers = { hasPendingChunks: () => false, flushChunks: () => {} } as unknown as StreamBuffers
const slotListSync = () => renderHook(() => useSlotListSync(store.dispatch, queryClient, buffers)).result.current
const unread = () => store.getState().dashboard.unreadSlots

describe('applyServerReads', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    __resetSharedUnreadQueueForTests()
    _resetSlotReadRelayForTest()
    sent.length = 0
    bindSlotReadSender((slot, ts) => { sent.push([slot, ts]) })
    queryClient.clear()
    store.dispatch(setActiveSlot(null))
    for (const key of [...store.getState().dashboard.unreadSlots]) store.dispatch(markSlotRead(key))
  })
  afterEach(() => {
    store.dispatch(sseSlots([]))
    _resetSlotReadRelayForTest()
  })

  it('badges a session whose agent wrote after the last read, with no live frame', () => {
    apply([row('missed')])
    expect(unread()).toContain('missed')
    expect(store.getState().dashboard.unreadSince?.missed).toBe(AFTER)
  })

  it('leaves a session alone when the read covers its newest agent row', () => {
    apply([row('seen', { last_agent_ts: BEFORE })])
    expect(unread()).not.toContain('seen')
  })

  it('does nothing while the gateway has not read its store', () => {
    apply([row('unknown', { last_read_ts: null, read_epoch: null })])
    expect(unread()).not.toContain('unknown')
  })

  describe('the store epoch', () => {
    const restored = (key: string) => {
      store.dispatch(markSlotUnread({ slot: key, ts: BEFORE }))
      flushSharedUnread()
      expect(unread()).toContain(key)
      return localStorage.getItem('mc-unread-shared')
    }

    it('never clears a restored badge, though it is newer', () => {
      const shared = restored('upgraded')
      apply([row('upgraded', { last_read_ts: null, read_epoch: READ, last_agent_ts: BEFORE })])
      expect(unread()).toContain('upgraded')
      expect(localStorage.getItem('mc-unread-shared')).toBe(shared)
    })

    it('lets a real read at that time clear the same badge', () => {
      restored('read')
      apply([row('read', { last_read_ts: READ, read_epoch: READ, last_agent_ts: BEFORE })])
      expect(unread()).not.toContain('read')
    })

    it('badges no reply at or before it on a slot nobody read', () => {
      apply([row('old', { last_read_ts: null, read_epoch: READ, last_agent_ts: BEFORE })])
      expect(unread()).not.toContain('old')
    })

    it('badges a reply after it on a slot nobody read', () => {
      apply([row('new', { last_read_ts: null, read_epoch: READ, last_agent_ts: AFTER })])
      expect(unread()).toContain('new')
    })
  })

  it('skips the slot on screen, a muted slot, a crewmate thread, and a running turn under the attention setting', () => {
    store.dispatch(setActiveSlot('open'))
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    apply([
      row('open'),
      row('muted', { muted: true }),
      row('member-mate', { mode: 'member' }),
      row('running', { running: true }),
      row('done'),
    ])
    expect(unread()).toEqual(['done'])
  })

  it('badges a running turn that waits on the user under the attention setting', () => {
    localStorage.setItem(UNREAD_ON_ATTENTION_KEY, '1')
    apply([
      row('approval', { running: true, pending_approval: true }),
      row('question', { running: true, needs_input: true }),
    ])
    expect(unread()).toEqual(['approval', 'question'])
  })

  it('clears a badge a read on another device covers', () => {
    apply([row('elsewhere', { last_read_ts: BEFORE })])
    expect(unread()).toContain('elsewhere')
    apply([row('elsewhere', { last_read_ts: AFTER })])
    expect(unread()).not.toContain('elsewhere')
  })

  it('keeps a badge for a message newer than the read', () => {
    apply([row('newer', { last_read_ts: BEFORE })])
    store.dispatch(markSlotUnread({ slot: 'newer', ts: '2026-11-13T12:00:00+00:00' }))
    apply([row('newer', { last_read_ts: AFTER })])
    expect(unread()).toContain('newer')
  })

  it('re-badges a reply newer than the read that cleared the old badge', () => {
    apply([row('replied', { last_read_ts: BEFORE, last_agent_ts: READ })])
    expect(store.getState().dashboard.unreadSince?.replied).toBe(READ)
    apply([row('replied', { last_read_ts: READ, last_agent_ts: AFTER })])
    expect(unread()).toContain('replied')
    expect(store.getState().dashboard.unreadSince?.replied).toBe(AFTER)
  })

  it('never clears a manual mark-as-unread', () => {
    apply([row('manual', { last_agent_ts: BEFORE })])
    store.dispatch(markSlotUnread('manual'))
    expect(store.getState().dashboard.unreadSince?.manual).toBe(MANUAL_UNREAD)
    apply([row('manual', { last_read_ts: AFTER, last_agent_ts: BEFORE })])
    expect(unread()).toContain('manual')
  })

  it('counts a row this window showed on screen as read', () => {
    noteShownHere('watched', AFTER)
    apply([row('watched')])
    expect(unread()).not.toContain('watched')
  })

  describe('a read the gateway has not confirmed', () => {
    it('holds the badge off, and no slot list sends it', () => {
      queueRead('laggy', AFTER)
      slotListSync().onSlots({ type: 'slots' }, [row('laggy')], 'laggy-1')
      expect(unread()).not.toContain('laggy')
      slotListSync().onSlots({ type: 'slots' }, [row('laggy', { title: 'again' })], 'laggy-2')
      expect(sent).toEqual([])
    })

    it('is sent once per kept entry when a new socket opens', () => {
      queueRead('one', READ)
      queueRead('two', AFTER)
      slotListSync().resetForConnection()
      expect(sent).toEqual([['one', READ], ['two', AFTER]])
    })

    it('is sent again without an unsaved row that arrived after it was kept', () => {
      queueRead('perm', READ)
      noteUnsavedRowTs('perm', AFTER)
      slotListSync().resetForConnection()
      expect(sent).toEqual([['perm', READ]])
    })

    it('is forgotten once a slot row carries a read at least as new', () => {
      queueRead('confirmed', READ)
      apply([row('confirmed', { last_read_ts: READ, last_agent_ts: BEFORE })])
      expect(queuedReads()).toEqual({})
      slotListSync().resetForConnection()
      expect(sent).toEqual([])
    })
  })

  describe('a slot_patch frame', () => {
    const patchWith = (key: string) => {
      slotListSync().onSlotPatch({ slots: [{ key, title: 'renamed' }] } as SlotPatchFrame)
    }

    it('never badges from the older read and agent times its rows keep', () => {
      store.dispatch(sseSlots([row('echoed', { last_read_ts: BEFORE })]))
      patchWith('echoed')
      expect(unread()).not.toContain('echoed')
    })

    it('leaves a badge a kept read covers to the next list', () => {
      apply([row('kept', { last_read_ts: BEFORE })])
      expect(unread()).toContain('kept')
      queueRead('kept', AFTER)
      patchWith('kept')
      expect(unread()).toContain('kept')
      apply([row('kept', { last_read_ts: BEFORE, title: 'listed' })])
      expect(unread()).not.toContain('kept')
    })
  })
})
