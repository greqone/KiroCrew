import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery } from '@tanstack/react-query'

import { api } from '../api/client'
import type { MemoryBenchPlanRow, MemoryBenchRequest } from '../api/client/memory'
import { registerMemoryBenchSheetHost, type MemoryBenchOutcome } from '../app-sdk/memoryBench'
import { fmtNumber } from '../i18n/format'
import ErrorNotice from './ErrorNotice'
import Modal from './Modal'
import { Badge, Btn } from './ui'

const RUNNING = new Set(['running'])

function ProviderRow({ row }: { row: MemoryBenchPlanRow }) {
  const { t } = useTranslation()
  const na = Object.keys(row.na)
  return (
    <li className="flex flex-col gap-1 py-2 border-t border-[var(--border)]">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-text-strong">{row.name}</span>
        <Badge variant={row.egress === 'remote' ? 'warn' : 'muted'}>
          {row.egress === 'remote' ? t('pages.overview.memoryBench.egress_remote') : t('pages.overview.memoryBench.egress_local')}
        </Badge>
        <Badge variant="muted">
          {row.cleanup === 'reset' ? t('pages.overview.memoryBench.cleanup_reset') : t('pages.overview.memoryBench.cleanup_delete')}
        </Badge>
      </div>
      <p className="text-muted text-[12px]">
        {row.ingest_mode === 'replay'
          ? t('pages.overview.memoryBench.replays', { n: fmtNumber(row.turns) })
          : t('pages.overview.memoryBench.writes', { n: fmtNumber(row.writes) })}
      </p>
      {na.length > 0 && (
        <p className="text-muted text-[12px]">{t('pages.overview.memoryBench.not_measured', { dims: na.join(', ') })}</p>
      )}
    </li>
  )
}

/**
 * The benchmark sheet: plans a run (a dry run), shows each provider's egress, cleanup
 * and what cannot be measured, the reader and judge call budget, and starts the run
 * only on the owner's press. A provider that keeps memory off this machine needs the
 * owner's tick for a copy of their own memory; the gateway checks the item count too.
 */
export function MemoryBenchSheet({
  request,
  onDone,
}: {
  request: MemoryBenchRequest
  onDone: (outcome: MemoryBenchOutcome) => void
}) {
  const { t } = useTranslation()
  const [acked, setAcked] = useState(false)
  const [runId, setRunId] = useState('')
  const planMut = useMutation({ mutationFn: () => api.startMemoryBench({ ...request, dry_run: true }) })
  const plan = planMut.data?.plan
  const own = request.suite.startsWith('own:')
  const remote = (plan?.providers ?? []).filter(p => p.egress === 'remote')
  const needsAck = own && remote.length > 0
  const runMut = useMutation({
    mutationFn: () =>
      api.startMemoryBench({
        ...request,
        dry_run: false,
        egress_ack: needsAck
          ? Object.fromEntries(remote.map(p => [p.id, { app_version: p.version, items: plan?.items ?? 0 }]))
          : undefined,
      }),
    onSuccess: started => setRunId(started.run_id),
  })
  const runQuery = useQuery({
    queryKey: ['memory-bench-run', runId],
    queryFn: () => api.memoryBenchRun(runId),
    enabled: !!runId,
    refetchInterval: query => (query.state.data && !RUNNING.has(query.state.data.state) ? false : 1500),
  })
  const started = useRef(false)
  useEffect(() => {
    if (started.current) return
    started.current = true
    planMut.mutate()
  }, [planMut])
  const run = runQuery.data
  const close = () => onDone(runId ? { outcome: 'started', runId } : { outcome: 'cancelled' })
  const progress = run ? Object.values(run.progress) : []
  const done = progress.reduce((n, p) => n + p.done, 0)
  const total = progress.reduce((n, p) => n + p.total, 0)
  const failed = planMut.isError || runMut.isError || runQuery.isError || run?.state === 'failed'
  const overBudget = !!plan && plan.llm_calls_estimate > plan.max_llm_calls
  return (
    <Modal
      open
      onClose={close}
      title={t('pages.overview.memoryBench.title')}
      maxWidth={600}
      layer="top"
      dismissDisabled={runMut.isPending}
      footer={
        <div className="flex justify-end gap-2">
          <Btn onClick={close} disabled={runMut.isPending}>
            {t('pages.overview.memoryProvider.close')}
          </Btn>
          {!runId && (
            <Btn
              primary
              busy={runMut.isPending}
              disabled={!plan || (needsAck && !acked)}
              onClick={() => runMut.mutate()}
            >
              {t('pages.overview.memoryBench.run')}
            </Btn>
          )}
        </div>
      }
    >
      <div className="flex flex-col gap-3 text-sm" data-testid="memory-bench-sheet">
        <p>{t('pages.overview.memoryBench.body')}</p>
        {planMut.isPending && <p className="text-muted">{t('pages.overview.memoryBench.planning')}</p>}
        {plan && (
          <>
            <p>
              {t('pages.overview.memoryBench.suite_line', {
                name: plan.suite.name,
                license: plan.suite.license,
                queries: fmtNumber(plan.queries),
              })}
            </p>
            {plan.suite.restricted && plan.suite.license_note && (
              <p className="text-muted text-[12px]">{plan.suite.license_note}</p>
            )}
            <ul className="flex flex-col">
              {plan.providers.map(row => (
                <ProviderRow key={row.id} row={row} />
              ))}
            </ul>
            <p className="text-muted text-[12px]">
              {t('pages.overview.memoryBench.llm_calls', {
                n: fmtNumber(plan.llm_calls_estimate),
                max: fmtNumber(plan.max_llm_calls),
              })}
            </p>
            {overBudget && <p className="text-warn-text text-[12px]">{t('pages.overview.memoryBench.over_budget')}</p>}
            {needsAck && !runId && (
              <label className="flex items-start gap-2 text-[12px]" htmlFor="memory-bench-ack">
                <input
                  id="memory-bench-ack"
                  type="checkbox"
                  checked={acked}
                  aria-label={t('pages.overview.memoryBench.ack', {
                    names: remote.map(p => p.name).join(', '),
                    n: fmtNumber(plan.items),
                  })}
                  onChange={e => setAcked(e.target.checked)}
                />
                <span aria-hidden="true">
                  {t('pages.overview.memoryBench.ack', {
                    names: remote.map(p => p.name).join(', '),
                    n: fmtNumber(plan.items),
                  })}
                </span>
              </label>
            )}
            <p className="text-muted text-[12px]">{t('pages.overview.memoryBench.scratch_only')}</p>
          </>
        )}
        {run && RUNNING.has(run.state) && (
          <p className="text-muted" role="status">
            {t('pages.overview.memoryBench.running', { done: fmtNumber(done), total: fmtNumber(total) })}
          </p>
        )}
        {run?.state === 'done' && <p role="status">{t('pages.overview.memoryBench.done')}</p>}
        {run?.state === 'budget_exhausted' && <p role="status">{t('pages.overview.memoryBench.budget_exhausted')}</p>}
        {failed && <ErrorNotice variant="inline" askAgent message={t('pages.overview.memoryBench.failed')} />}
      </div>
    </Modal>
  )
}

/**
 * Mounts the sheet `requestMemoryBench()` opens. One per window; an ask that arrives
 * while one is open answers `'cancelled'`.
 */
export function MemoryBenchSheetHost() {
  const [request, setRequest] = useState<MemoryBenchRequest | null>(null)
  const settle = useRef<((outcome: MemoryBenchOutcome) => void) | null>(null)
  useEffect(() => {
    const off = registerMemoryBenchSheetHost(next => new Promise(resolve => {
      if (settle.current) {
        resolve({ outcome: 'cancelled' })
        return
      }
      settle.current = resolve
      setRequest(next)
    }))
    return () => {
      off()
      settle.current?.({ outcome: 'cancelled' })
      settle.current = null
    }
  }, [])
  if (!request) return null
  return (
    <MemoryBenchSheet
      request={request}
      onDone={outcome => {
        settle.current?.(outcome)
        settle.current = null
        setRequest(null)
      }}
    />
  )
}
