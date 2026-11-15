// The benchmark sheet and the App SDK call that opens it.
//
// Pinned here: `requestMemoryBench()` starts nothing by itself (no host or a bad
// request -> 'unavailable'); the sheet plans first (a dry run), shows egress and
// cleanup per provider, needs the owner's tick before own memory leaves the
// machine, and starts the run only on the owner's press.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { api } from '../api/client'
import type { MemoryBenchPlan } from '../api/client/memory'
import { requestMemoryBench } from '../app-sdk/memoryBench'
import { MemoryBenchSheetHost } from './MemoryBenchSheet'
import { i18nT } from '../i18n/t'

function plan(egress: 'local' | 'remote'): MemoryBenchPlan {
  return {
    suite: { id: 'own', name: 'Your own memory', license: 'yours', license_note: '', restricted: false },
    units: 1,
    queries: 40,
    items: 120,
    llm_calls_estimate: 0,
    max_llm_calls: 2000,
    providers: [
      {
        id: 'app:cloud/mem',
        name: 'Cloud memory',
        egress,
        version: '1.4.0',
        cleanup: 'reset',
        ingest_mode: 'items',
        ingest_why: '',
        writes: 120,
        turns: 0,
        na: { M3: 'measured without as_of' },
      },
    ],
  }
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryBenchSheetHost />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('requestMemoryBench', () => {
  it('answers unavailable with no host and for a malformed request', async () => {
    await expect(requestMemoryBench({ suite: 'own:e_1', providers: ['builtin'] })).resolves.toEqual({ outcome: 'unavailable' })
    mount()
    await expect(requestMemoryBench({ suite: '', providers: ['builtin'] })).resolves.toEqual({ outcome: 'unavailable' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('needs the owner tick before a copy of own memory leaves, then starts on the press', async () => {
    const start = vi
      .spyOn(api, 'startMemoryBench')
      .mockResolvedValueOnce({ run_id: 'r_1', state: 'planned', plan: plan('remote') })
      .mockResolvedValueOnce({ run_id: 'r_2', state: 'running', plan: plan('remote') })
    vi.spyOn(api, 'memoryBenchRun').mockResolvedValue({
      id: 'r_2', state: 'done', plan: plan('remote'), progress: {}, warnings: [], error: '',
    })
    mount()
    let outcome: Promise<unknown> | undefined
    act(() => {
      outcome = requestMemoryBench({ suite: 'own:e_1', providers: ['app:cloud/mem'], dry_run: false })
    })
    await screen.findByTestId('memory-bench-sheet')
    await waitFor(() => expect(start).toHaveBeenCalledWith({ suite: 'own:e_1', providers: ['app:cloud/mem'], dry_run: true }))
    await screen.findByText(i18nT('pages.overview.memoryBench.egress_remote'))
    const run = screen.getByRole('button', { name: i18nT('pages.overview.memoryBench.run') })
    expect(run).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(run)
    await waitFor(() =>
      expect(start).toHaveBeenLastCalledWith({
        suite: 'own:e_1',
        providers: ['app:cloud/mem'],
        dry_run: false,
        egress_ack: { 'app:cloud/mem': { app_version: '1.4.0', items: 120 } },
      }),
    )
    await screen.findByText(i18nT('pages.overview.memoryBench.done'))
    const closes = screen.getAllByRole('button', { name: i18nT('pages.overview.memoryProvider.close') })
    fireEvent.click(closes[closes.length - 1])
    await expect(outcome).resolves.toEqual({ outcome: 'started', runId: 'r_2' })
  })

  it('asks for no tick when every provider stays local', async () => {
    vi.spyOn(api, 'startMemoryBench').mockResolvedValue({ run_id: 'r_1', state: 'planned', plan: plan('local') })
    mount()
    act(() => {
      void requestMemoryBench({ suite: 'own:e_1', providers: ['app:cloud/mem'] })
    })
    const run = await screen.findByRole('button', { name: i18nT('pages.overview.memoryBench.run') })
    await waitFor(() => expect(run).not.toBeDisabled())
    expect(screen.queryByRole('checkbox')).toBeNull()
  })
})
