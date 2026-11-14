/**
 * Settings -> Security -> Safety switches: the owner's toggles for the two MCP
 * safety keys, `mcp.protect_kiro_permission_rules` and `mcp.honour_auto_approve`
 * (`SecurityPanel.tsx` mounts `SafetySwitchesSection`).
 *
 * A VIEW over `/api/security/safety-switches`, which is owner-only on both verbs
 * and the only dashboard writer of the two keys (the Advanced page shows them
 * read-only and links here). A non-owner's read is refused, so their rows are
 * drawn disabled from the config every dashboard session already reads, with
 * a neutral owner-only line. A failed read draws no switch: a default shown for a
 * state we could not read may be the reassuring wrong one.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Lock, RotateCw } from 'lucide-react'
import { retryPolicy } from '../../api/queryClient'
import { Btn, SkeletonToggleRow } from '../../components/ui'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api, type SafetySwitchKey, type SafetySwitchesState } from '../../api/client'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import { isOwnerGateRefusal } from './SecurityPanelRedaction'

const PROTECT: SafetySwitchKey = 'mcp.protect_kiro_permission_rules'
const HONOUR: SafetySwitchKey = 'mcp.honour_auto_approve'

/** The slice of `/api/config/kirocrew` a non-owner's disabled rows read. */
interface McpCfgShape { mcp?: { protect_kiro_permission_rules?: unknown; honour_auto_approve?: unknown } }

interface Row { enabled: boolean; in_force: boolean }

function SafetySwitchesCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError, error, isFetching, refetch } = useQuery<SafetySwitchesState>({
    queryKey: ['safety-switches'],
    queryFn: api.safetySwitches,
    // An owner-gate refusal is a fact about the viewer: a retry would only
    // write another audited refusal.
    retry: (count, err) => !isOwnerGateRefusal(err) && retryPolicy(count, err),
  })
  const ownerGateRefused = isError && isOwnerGateRefusal(error)
  // Shared cache entry with the Security rail, so a non-owner adds no request.
  const { data: cfg } = useQuery<McpCfgShape>({
    queryKey: ['kirocrewConfig'],
    queryFn: api.kirocrewConfig,
    enabled: ownerGateRefused,
  })
  const set = useMutation({
    mutationFn: ({ key, enabled }: { key: SafetySwitchKey; enabled: boolean }) => api.setSafetySwitch(key, enabled),
    onSuccess: state => qc.setQueryData(['safety-switches'], state),
    // Re-read on every settlement: a PUT that landed but lost its response
    // must not leave the old position on screen. The config cache holds the
    // same keys, so it is refreshed too.
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['safety-switches'] })
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
  })

  // `isError`, not `data === undefined`: react-query keeps the last good data
  // across a rejected refetch.
  const view = isError ? undefined : data
  const rows = new Map<SafetySwitchKey, Row>()
  if (view) {
    for (const s of view.switches) rows.set(s.key, { enabled: s.enabled, in_force: s.in_force })
  } else if (ownerGateRefused && cfg?.mcp) {
    const { protect_kiro_permission_rules: p, honour_auto_approve: h } = cfg.mcp
    if (typeof p === 'boolean') rows.set(PROTECT, { enabled: p, in_force: p })
    if (typeof h === 'boolean') rows.set(HONOUR, { enabled: h, in_force: h })
  }
  const readOnly = !view
  const busy = set.isPending
  // `mcp.honour_auto_approve` is read when the agent spec is rebuilt
  // (governance.strip_ungoverned_auto_approve, run by install_agent at start),
  // and no config subscriber rebuilds it on a save, so its flip needs a restart.
  // The protect fence reads the live config at each file edit: no hint.
  const honourSaved = set.isSuccess && set.variables?.key === HONOUR

  // Both toggles read ON = safer. The ask toggle is the inverse of
  // `mcp.honour_auto_approve`; the config key and the API are unchanged.
  const shown = (key: SafetySwitchKey, row: Row) =>
    key === HONOUR ? { checked: !row.enabled, inForce: !row.in_force } : { checked: row.enabled, inForce: row.in_force }

  const toggleRow = (key: SafetySwitchKey, testId: string, labelKey: string, descKey: string) => {
    const row = rows.get(key)
    if (!row) return null
    const { checked, inForce } = shown(key, row)
    return (
      <div data-testid={testId} className="flex flex-col gap-1 border border-border rounded-md px-3 py-2">
        <SettingsToggle
          label={i18nT(labelKey)}
          description={i18nT(descKey)}
          configKey={key}
          checked={checked}
          disabled={readOnly || busy}
          onChange={on => set.mutate({ key, enabled: key === HONOUR ? !on : on })}
        />
        {inForce !== checked && (
          <div className="flex items-start gap-2 text-[11px] text-warn leading-relaxed">
            <AlertTriangle size={13} className="shrink-0 mt-px" />
            <span>{i18nT(inForce ? 'pages.settings.securityPanel.safetySwitches.in_force_on' : 'pages.settings.securityPanel.safetySwitches.in_force_off')}</span>
          </div>
        )}
        {key === HONOUR && honourSaved && (
          <div data-testid="safety-switch-restart-hint" className="flex items-start gap-2 text-[11px] text-muted leading-relaxed">
            <RotateCw size={12} className="shrink-0 mt-px" />
            <span>{i18nT('pages.settings.securityPanel.safetySwitches.restart_hint')}</span>
          </div>
        )}
      </div>
    )
  }

  return (
    <SettingsCard>
      <p className="m-0 mb-2 text-[12px] text-muted leading-relaxed">
        {i18nT('pages.settings.securityPanel.safetySwitches.intro')}
      </p>
      {isLoading ? (
        <div data-testid="safety-switches-loading" aria-busy="true">
          <SkeletonToggleRow />
          <SkeletonToggleRow />
        </div>
      ) : view || ownerGateRefused ? (
        <div data-testid="safety-switches-rows" className="flex flex-col gap-1.5">
          {ownerGateRefused && (
            // A permission lock, not a failure: a neutral line with nothing to retry.
            <div data-testid="safety-switches-owner-only" className="flex items-start gap-2 text-[12px] text-muted leading-relaxed">
              <Lock size={13} className="shrink-0 mt-px" />
              <span>{i18nT('pages.settings.securityPanel.safetySwitches.owner_only')}</span>
            </div>
          )}
          {toggleRow(PROTECT, 'safety-switch-protect', 'pages.settings.securityPanel.safetySwitches.protect_label', 'pages.settings.securityPanel.safetySwitches.protect_desc')}
          {toggleRow(HONOUR, 'safety-switch-honour', 'pages.settings.securityPanel.safetySwitches.ask_label', 'pages.settings.securityPanel.safetySwitches.ask_desc')}
          {view?.degraded && (
            <div data-testid="safety-switches-degraded" className="flex items-start gap-2 text-[11px] text-warn leading-relaxed">
              <AlertTriangle size={13} className="shrink-0 mt-px" />
              <span>{i18nT('pages.settings.securityPanel.safetySwitches.degraded')}</span>
            </div>
          )}
          {set.isError && (
            <ErrorNotice
              variant="inline"
              className="mt-1"
              message={i18nT('pages.settings.securityPanel.safetySwitches.write_failed')}
              askAgent
              askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
              testId="safety-switches-write-failed"
            />
          )}
        </div>
      ) : isError ? (
        <ErrorNotice
          message={i18nT('pages.settings.securityPanel.safetySwitches.read_failed')}
          askAgent
          askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
          testId="safety-switches-read-failed"
          footer={
            <Btn onClick={() => { void refetch() }} disabled={isFetching} data-testid="safety-switches-retry">
              {i18nT('pages.settings.securityPanel.credential_redaction_retry')}
            </Btn>
          }
        />
      ) : null}
    </SettingsCard>
  )
}

/** Settings -> Security -> Safety switches. */
export function SafetySwitchesSection() {
  return (
    <SettingsSection title={i18nT('pages.settings.securityPanel.safetySwitches.section_title')}>
      <SafetySwitchesCard />
    </SettingsSection>
  )
}
