import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { MemberRosterRow } from '../../api/client'
// Vite's `?raw`: the component's own text, for the declared hand-off decision.
import groupSource from './CrewStartersGroup.tsx?raw'

vi.mock('../../api/client', () => ({
  api: { updateKirocrewAgent: vi.fn().mockResolvedValue({ ok: true }) },
}))

import { api } from '../../api/client'
import CrewStartersGroup from './CrewStartersGroup'

const update = api.updateKirocrewAgent as ReturnType<typeof vi.fn>

function renderGroup(starters?: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const member = { name: 'radar', starters } as unknown as MemberRosterRow
  return render(
    <QueryClientProvider client={qc}>
      <CrewStartersGroup member={member} name="Radar" />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  update.mockResolvedValue({ ok: true })
})

describe('the Bot profile starters box', () => {
  it('shows the saved starters one per line and keeps Save off until something changes', () => {
    renderGroup(['Plan my week', 'Summarize my inbox'])
    expect(screen.getByTestId('crew-profile-starters-input')).toHaveValue('Plan my week\nSummarize my inbox')
    expect(screen.getByTestId('crew-profile-starters-save')).toBeDisabled()
  })

  it('saves the non-blank lines, trimmed, as the starters list', async () => {
    renderGroup([])
    fireEvent.change(screen.getByTestId('crew-profile-starters-input'), { target: { value: '  Plan my week \n\n Summarize  ' } })
    fireEvent.click(screen.getByTestId('crew-profile-starters-save'))
    await waitFor(() => expect(update).toHaveBeenCalledWith('radar', { starters: ['Plan my week', 'Summarize'] }))
  })

  it('after a partial save shows only what was kept, says so first, and gives each line left out its reason', async () => {
    // The kept set equals the saved one, so no roster change can reset the box.
    update.mockResolvedValue({
      ok: true,
      starters: ['Plan my week', 'Summarize my inbox'],
      starters_left_out: [{ place: 2, reason: 'repeated' }, { place: 3, reason: 'too_long' }, { place: 5, reason: 'credential' }],
      warnings: ['starter 2 repeats an earlier starter and was left out'],
    })
    renderGroup(['Plan my week', 'Summarize my inbox'])
    const box = screen.getByTestId('crew-profile-starters-input')
    const long = 'Walk me through every open review '.repeat(4).trim()
    fireEvent.change(box, { target: { value: `Plan my week\nPlan my week\n${long}\nSummarize my inbox\nUse token abc` } })
    fireEvent.click(screen.getByTestId('crew-profile-starters-save'))
    const note = await screen.findByTestId('crew-profile-starters-left-out')
    expect(box).toHaveValue('Plan my week\nSummarize my inbox')
    expect(screen.getByTestId('crew-profile-starters-save')).toBeDisabled()
    // What happened comes first; the rule list is gone.
    expect(within(note).getByRole('paragraph')).toHaveTextContent(/^Saved 2 starters\. Each line left out is listed with its reason\.$/)
    expect(note).not.toHaveTextContent(/Not saved/)
    expect(within(note).getAllByTestId('crew-profile-starters-left-out-line').map((li) => li.textContent))
      .toEqual([
        'Repeated: Plan my week',
        `Too long, over 120 characters: ${long}`,
        'Looks like a password or key: Use token abc',
      ])
    // Right under Save, inside the same card, not after the caption.
    expect(screen.getByTestId('crew-profile-starters')).toContainElement(note)
  })

  it('words one saved starter in the singular, and lists a line left out for an unknown reason without one', async () => {
    update.mockResolvedValue({ ok: true, starters: ['Plan my week'], starters_left_out: [{ place: 2, reason: 'something_new' }] })
    renderGroup([])
    fireEvent.change(screen.getByTestId('crew-profile-starters-input'), { target: { value: 'Plan my week\nOdd one' } })
    fireEvent.click(screen.getByTestId('crew-profile-starters-save'))
    const note = await screen.findByTestId('crew-profile-starters-left-out')
    expect(within(note).getByRole('paragraph')).toHaveTextContent(/^Saved 1 starter\. Each line left out is listed with its reason\.$/)
    expect(within(note).getAllByTestId('crew-profile-starters-left-out-line').map((li) => li.textContent)).toEqual(['Odd one'])
  })

  it('says nothing was left out when the save kept every line', async () => {
    update.mockResolvedValue({ ok: true, starters: ['ok'], starters_left_out: [] })
    renderGroup([])
    fireEvent.change(screen.getByTestId('crew-profile-starters-input'), { target: { value: 'ok' } })
    fireEvent.click(screen.getByTestId('crew-profile-starters-save'))
    await waitFor(() => expect(update).toHaveBeenCalled())
    await waitFor(() => expect(screen.getByTestId('crew-profile-starters-input')).toHaveValue('ok'))
    expect(screen.queryByTestId('crew-profile-starters-left-out')).toBeNull()
  })

  it('reports a failed save', async () => {
    update.mockRejectedValue(new Error('boom'))
    renderGroup([])
    fireEvent.change(screen.getByTestId('crew-profile-starters-input'), { target: { value: 'ok' } })
    fireEvent.click(screen.getByTestId('crew-profile-starters-save'))
    expect(await screen.findByTestId('crew-profile-starters-error')).toBeInTheDocument()
  })

  it('offers no hand-off on a failed save, so the unsaved draft stays in the box, and says why in the source', async () => {
    update.mockRejectedValue(new Error('boom'))
    renderGroup([])
    fireEvent.change(screen.getByTestId('crew-profile-starters-input'), { target: { value: 'Plan my week' } })
    fireEvent.click(screen.getByTestId('crew-profile-starters-save'))
    const notice = await screen.findByTestId('crew-profile-starters-error')
    expect(within(notice).queryByRole('button', { name: /ask the agent/i })).toBeNull()
    expect(screen.getByTestId('crew-profile-starters-input')).toHaveValue('Plan my week')
    // The rule asks the omission to be declared next to the notice, naming the draft.
    const beforeNotice = groupSource.slice(0, groupSource.indexOf('<ErrorNotice'))
    expect(beforeNotice.slice(beforeNotice.lastIndexOf('</RowGroup>'))).toMatch(/No hand-off:[^}]*starter draft/)
  })
})
