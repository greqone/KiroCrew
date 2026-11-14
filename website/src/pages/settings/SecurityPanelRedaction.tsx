/**
 * Settings -> Security -> Redaction: the owner's one credential-redaction toggle, and
 * the per-workspace hosts allowed long queries (`SecurityPanel.tsx` re-exports
 * `RedactionSection` and `RedactionAllowedHostsCard`).
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { retryPolicy } from '../../api/queryClient'
import { STALE_OWNER_SESSION_CODE } from '../../api/staleOwnerSignal'
import { purgeDocumentBodiesForRedactionChange } from '../../hooks/usePanelTabs'
import { Btn, SkeletonToggleRow } from '../../components/ui'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'
import { api, ApiError, type CredentialRedactionState } from '../../api/client'
import { i18nT } from '../../i18n/t'
import ErrorNotice from '../../components/ErrorNotice'
import { reportForError } from '../../utils/errorReport'

/* ── Credential redaction toggle ─────────────────────────────────────────────
 *
 * A VIEW over the one setting the `/api/security/credential-redaction` endpoints own,
 * drawn like the Browser settings: one card, one toggle, one short sentence under the label.
 * Off by default (the backend's `DEFAULT_REDACTION_ENABLED`); the server decides what
 * the setting reaches, so this component carries no list of paths.
 *
 * A FAILED READ renders NO toggle. Rendering a default for a state we could not read
 * would show a position that may be wrong.
 */
/** The owner gate refusing the viewer: a 403 that is NOT the token-auth
 *  middleware's own re-auth denial (`X-Auth-Required`, a mid-session cookie
 *  lapse the transport refreshes silently -- that one must stay a retryable
 *  read), or the backend's `stale_session_reauth` code (a session that predates
 *  the owner claim). Neither is a transient, so neither gets a retry or a Retry
 *  button; `FilePathMenu` draws the same 403-vs-authRequired line. */
function isOwnerGateRefusal(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  if (error.status === 403 && !error.authRequired) return true
  try {
    const code = (JSON.parse(error.body || '{}') as { code?: unknown }).code
    return code === STALE_OWNER_SESSION_CODE
  } catch { return false }
}

function useCredentialRedactionRead() {
  return useQuery<CredentialRedactionState>({
    queryKey: ['credential-redaction'],
    queryFn: api.credentialRedaction,
    // An owner-gated refusal is a fact about the viewer, not a transient:
    // retrying would only write a second audited refusal for the same open
    // (see `isOwnerGateRefusal` for what counts; a re-auth 403 does not).
    retry: (count, error) => !isOwnerGateRefusal(error) && retryPolicy(count, error),
  })
}

function CredentialRedactionCard() {
  const qc = useQueryClient()
  const { data, isLoading, isError, error, isFetching, refetch } = useCredentialRedactionRead()
  const set = useMutation({
    mutationFn: (enabled: boolean) => api.setCredentialRedaction(enabled),
    onSuccess: state => {
      qc.setQueryData(['credential-redaction'], state)
      // DROP every file body THIS document holds (react-query bodies and open
      // tab bodies -- see `purgeDocumentBodiesForRedactionChange`) on a write
      // the server ACCEPTED. Other open dashboard documents purge on the
      // `credential_redaction_changed` push.
      purgeDocumentBodiesForRedactionChange(qc)
    },
    // Re-read on EVERY settlement, failure included: a PUT whose write landed
    // but whose response was lost would otherwise leave the toggle showing the
    // position from before the click while the setting is already in force.
    onSettled: () => void qc.invalidateQueries({ queryKey: ['credential-redaction'] }),
  })
  // react-query RETAINS the last good `data` across a rejected refetch, so
  // `isError` -- not `data === undefined` -- is the whole test for "unreadable".
  const view = isError ? undefined : data
  const ownerGateRefused = isError && isOwnerGateRefusal(error)

  return (
    <SettingsCard>
      {isLoading ? (
        <div data-testid="credential-redaction-loading" aria-busy="true">
          <SkeletonToggleRow />
        </div>
      ) : view ? (
        <>
          <SettingsToggle
            label={i18nT('pages.settings.securityPanel.credential_redaction_toggle')}
            description={i18nT('pages.settings.securityPanel.credential_redaction_toggle_help')}
            checked={view.enabled}
            disabled={set.isPending}
            onChange={enabled => set.mutate(enabled)}
          />
          {/* A rejected PUT is an error the agent can usually fix (an owner-gate
              refusal, a keystone write failure, a policy that keeps redaction on). */}
          {set.isError && (
            <ErrorNotice
              variant="inline"
              className="mt-1"
              message={i18nT('pages.settings.securityPanel.credential_redaction_write_failed', { state: i18nT(view.enabled ? 'pages.settings.securityPanel.credential_redaction_still_on' : 'pages.settings.securityPanel.credential_redaction_still_off') })}
              askAgent
              askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
              testId="credential-redaction-write-failed"
            />
          )}
        </>
      ) : ownerGateRefused ? (
        // A non-owner: not a failure to recover from, a fact about the viewer. No Retry
        // (it can never succeed and each click would write another audited refusal).
        <ErrorNotice
          variant="inline"
          message={i18nT('pages.settings.securityPanel.credential_redaction_owner_only')}
          askAgent
          askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
          testId="credential-redaction-owner-only"
        />
      ) : isError ? (
        // With the read failed there is no toggle to move, so the notice carries the
        // only recovery that does not mean reloading the page: the query's own retry.
        <ErrorNotice
          message={i18nT('pages.settings.securityPanel.credential_redaction_read_failed')}
          askAgent
          askAgentLabel={i18nT('pages.settings.securityPanel.credential_redaction_ask_agent')}
          testId="credential-redaction-read-failed"
          footer={
            <Btn onClick={() => { void refetch() }} disabled={isFetching} data-testid="credential-redaction-retry">
              {i18nT('pages.settings.securityPanel.credential_redaction_retry')}
            </Btn>
          }
        />
      ) : null}
    </SettingsCard>
  )
}

/* ── Redaction: hosts allowed long queries, per workspace ── */
export function RedactionAllowedHostsCard() {
  const queryClient = useQueryClient()
  const { data, isLoading, error } = useQuery({
    queryKey: ['redaction-allowed-hosts'],
    queryFn: () => api.redactionAllowedHosts(),
  })
  const revoke = useMutation({
    mutationFn: ({ workspace, host }: { workspace: string; host: string }) => api.redactionRevokeHost(workspace, host),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['redaction-allowed-hosts'] }),
  })
  const rows = Object.entries(data?.workspaces ?? {}).flatMap(([workspace, hosts]) => hosts.map(host => ({ workspace, host })))
  return (
    <SettingsCard>
      <div className="mb-3 text-[12px] leading-relaxed text-muted" data-testid="redaction-settings-note">
        {i18nT('pages.settings.securityPanel.redaction_always_on')}
      </div>
      <ErrorNotice
        variant="inline"
        askAgent
        message={error ? i18nT('pages.settings.securityPanel.redaction_load_failed') : null}
        report={error ? reportForError(error) : undefined}
      />
      <ErrorNotice
        variant="inline"
        askAgent
        message={revoke.isError ? i18nT('pages.settings.securityPanel.redaction_revoke_failed') : null}
        report={revoke.isError ? reportForError(revoke.error) : undefined}
        onDismiss={() => revoke.reset()}
      />
      {!isLoading && !error && rows.length === 0 && (
        <div className="text-[12px] text-muted" data-testid="redaction-allowed-empty">
          {i18nT('pages.settings.securityPanel.redaction_no_allowed_hosts')}
        </div>
      )}
      {rows.length > 0 && (
        <div className="divide-y divide-border" data-testid="redaction-allowed-list">
          {rows.map(({ workspace, host }) => (
            <div key={`${workspace}\u0000${host}`} className="flex items-center justify-between gap-3 py-2">
              <div className="min-w-0">
                <div className="truncate font-mono text-[13px] text-text">{host}</div>
                <div className="text-[11px] text-muted">
                  {i18nT('pages.settings.securityPanel.redaction_workspace', { workspace })}
                </div>
              </div>
              <Btn
                danger
                disabled={revoke.isPending}
                onClick={() => revoke.mutate({ workspace, host })}
                data-testid="redaction-revoke"
              >
                {i18nT('pages.settings.securityPanel.redaction_revoke')}
              </Btn>
            </div>
          ))}
        </div>
      )}
    </SettingsCard>
  )
}

/** Settings -> Security -> Redaction: the one toggle, then the allowed link hosts. */
export function RedactionSection() {
  return (
    <>
      <SettingsSection title={i18nT('pages.settings.securityPanel.credential_redaction_section')}>
        <CredentialRedactionCard />
      </SettingsSection>
      <SettingsSection title={i18nT('pages.settings.securityPanel.redaction_allowed_hosts_section')}>
        <RedactionAllowedHostsCard />
      </SettingsSection>
    </>
  )
}
