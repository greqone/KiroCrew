/**
 * MarketRegistryManager — the "Kiro Space" block of the Sources popover.
 *
 * Lists the Kiro Space stores this build offers (`GET /api/apps/market/registries`)
 * and lets the owner add or remove one, and sign in or out of an added one. The
 * person never types an address: only a store the build offers can be added.
 * With no offers (the open-source build has no store provider) the block renders
 * nothing at all.
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Trans, useTranslation } from 'react-i18next'
import { ExternalLink, Store } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import { safeHref } from '../../utils/safeHref'
import type { MarketRegistryOffer, MarketRegistryStatus, MarketSignInView } from './types'

export const MARKET_REGISTRIES_QUERY_KEY = ['market-registries'] as const

/** One line saying where a store's sign-in stands. */
export function useMarketStatusText() {
  const { t } = useTranslation()
  return (status: MarketRegistryStatus | undefined): string => {
    if (!status) return t('components.appstore.marketStore.state_checking')
    switch (status.state) {
      case 'signed_in':
        return t('components.appstore.marketStore.state_signed_in', { account: status.account })
      case 'needs_sign_in':
        return t('components.appstore.marketStore.state_needs_sign_in')
      case 'no_access':
        return t('components.appstore.marketStore.state_no_access')
      default:
        if (status.detail === 'cli_missing') return t('components.appstore.marketStore.state_cli_missing')
        if (status.detail === 'cli_outdated') return t('components.appstore.marketStore.state_cli_outdated')
        return t('components.appstore.marketStore.state_checking')
    }
  }
}

function StoreRow({ offer, added, status, onChanged }: {
  offer: MarketRegistryOffer
  added: string[]
  status?: MarketRegistryStatus
  onChanged: () => void
}) {
  const { t } = useTranslation()
  const statusText = useMarketStatusText()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [signIn, setSignIn] = useState<MarketSignInView | null>(null)
  const isAdded = added.includes(offer.id)

  const run = async (work: () => Promise<unknown>) => {
    setBusy(true)
    setError('')
    try {
      await work()
      onChanged()
    } catch (e) {
      setError((e as Error)?.message || t('components.appstore.marketStore.action_failed'))
    } finally {
      setBusy(false)
    }
  }
  const add = () => run(() => api.setMarketRegistries([...added, offer.id]))
  const remove = () => run(async () => {
    setSignIn(null)
    await api.setMarketRegistries(added.filter(id => id !== offer.id))
  })
  const startSignIn = () => run(async () => setSignIn(await api.marketSignIn(offer.id)))
  const signOut = () => run(async () => {
    setSignIn(null)
    await api.marketSignOut(offer.id)
  })

  const state = status?.state
  // `unknown` because the CLI is too old: signing in again cannot help.
  const outdated = state === 'unknown' && status?.detail === 'cli_outdated'
  const showSignIn = isAdded && !outdated && (state === 'needs_sign_in' || state === 'no_access')
  const showSignOut = isAdded && (state === 'signed_in' || state === 'no_access')
  const showRetry = isAdded && state === 'unknown' && !outdated
  const approvalPage = signIn?.approvalPage ? safeHref(signIn.approvalPage) : ''
  const signInEnded = signIn?.state === 'failed' || signIn?.state === 'expired'

  return (
    <div className="py-2 border-b border-border last:border-b-0" data-testid={`market-store-${offer.id}`}>
      <div className="flex items-center gap-2">
        <Store size={15} className="text-muted shrink-0" aria-hidden />
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-medium text-text-strong truncate">{offer.label}</div>
          {offer.web && <div className="text-[12px] text-muted truncate">{offer.web.replace(/^https:\/\//, '')}</div>}
          {isAdded && <div className="text-[12px] text-muted" role="status">{statusText(status)}</div>}
        </div>
        {!isAdded && <Btn disabled={busy} onClick={add}>{t('components.appstore.marketStore.add')}</Btn>}
        {showSignIn && <Btn primary disabled={busy} onClick={startSignIn}>{t('components.appstore.marketStore.sign_in')}</Btn>}
        {showRetry && <Btn disabled={busy} onClick={onChanged}>{t('components.appstore.marketStore.retry')}</Btn>}
        {showSignOut && <Btn disabled={busy} onClick={signOut}>{t('components.appstore.marketStore.sign_out')}</Btn>}
        {isAdded && <Btn disabled={busy} onClick={remove}>{t('components.appstore.marketStore.remove')}</Btn>}
      </div>
      {signIn?.code && !signInEnded && (
        <div className="mt-2 ml-6 text-[12.5px] flex items-center gap-3 flex-wrap">
          <span>
            <Trans
              i18nKey="components.appstore.marketStore.signin_code"
              components={{ mono: <code className="font-mono bg-bg-elevated px-1 rounded">{signIn.code}</code> }}
            />
          </span>
          {approvalPage && (
            <a href={approvalPage} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-accent">
              <ExternalLink size={12} className="lucide-inline" aria-hidden /> {t('components.appstore.marketStore.open_approval_page')}
            </a>
          )}
        </div>
      )}
      {signInEnded && (
        <p className="mt-2 ml-6 text-[12px] text-muted">{t('components.appstore.marketStore.signin_failed_hint')}</p>
      )}
      {error && <div className="mt-2"><ErrorNotice message={error} variant="inline" onDismiss={() => setError('')} askAgent /></div>}
    </div>
  )
}

export default function MarketRegistryManager() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: MARKET_REGISTRIES_QUERY_KEY,
    queryFn: () => api.getMarketRegistries(),
    staleTime: 30_000,
  })
  const offers = data?.offers ?? []
  if (offers.length === 0) return null
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: MARKET_REGISTRIES_QUERY_KEY })
    void queryClient.invalidateQueries({ queryKey: ['market-listings'] })
  }
  return (
    <div className="border-t border-border mt-4 pt-4" data-testid="market-registry-manager">
      <div className="text-sm font-semibold tracking-tight text-text-strong mb-1">{t('components.appstore.marketStore.title')}</div>
      <p className="text-[12px] text-muted mb-2">{t('components.appstore.marketStore.intro')}</p>
      {offers.map(offer => (
        <StoreRow
          key={offer.id}
          offer={offer}
          added={data?.added ?? []}
          status={data?.status[offer.id]}
          onChanged={refresh}
        />
      ))}
    </div>
  )
}
