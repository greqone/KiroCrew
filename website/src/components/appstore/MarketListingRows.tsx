/**
 * MarketListingRows — the apps of one added Kiro Space store, on the Discover page.
 *
 * Its own block, never mixed into the git registry list: every action here is
 * keyed by the store id and the listing ref, never by the app name, so a store
 * listing and a git row that share a name stay two cards that cannot trigger
 * each other's install. Install opens InstallFromSpaceDialog
 * (`POST /api/apps/market/install`); the row opens
 * `/apps/-/market/<store>/<namespace>/<name>`.
 */
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { BadgeCheck, Check, RefreshCw, Star } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../ErrorNotice'
import Clickable from '../Clickable'
import { Btn, IconButton } from '../ui'
import AppIconTile from './AppIconTile'
import InstallFromSpaceDialog from './InstallFromSpaceDialog'
import { MARKET_REGISTRIES_QUERY_KEY, useMarketStatusText } from './MarketRegistryManager'
import { fmtNumber } from '../../i18n/format'
import { cardDataKey } from '../../pages/apps/cardDataKey'
import type { MarketListingRow, MarketListingsResponse, MarketRegistryOffer } from './types'

/** Most rows the gateway reads from one store (10 pages of 100). */
export const MARKET_LISTING_LIMIT = 1000

export const marketListingsKey = (registry: string) => ['market-listings', registry] as const

/** The detail route of one store listing. `-/` keeps it off every app name. */
export function marketListingPath(row: Pick<MarketListingRow, 'registry' | 'ref'>): string {
  const [namespace, name] = row.ref.split('/')
  return `/apps/-/market/${encodeURIComponent(row.registry)}/${encodeURIComponent(namespace)}/${encodeURIComponent(name)}`
}

/** The read-error copy for a failed store read, by its code. */
export function readErrorKey(code: string): string {
  return code === 'market_api_refused'
    ? 'components.appstore.marketStore.err_read_refused'
    : 'components.appstore.marketStore.err_unreachable'
}

export function MarketRow({ row, label, onInstall }: {
  row: MarketListingRow
  label: string
  onInstall: (row: MarketListingRow) => void
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const rating = row.rating.status === 'ok' ? row.rating.value : null
  const reviews = row.ratingCount.status === 'ok' ? row.ratingCount.value : 0
  return (
    <Clickable
      aria-label={t('components.appstore.appListRow.view_details_for', { name: row.displayName })}
      className="flex items-center gap-3.5 px-3.5 py-3 border border-border rounded-xl bg-card mb-2 cursor-pointer hover:border-border-strong transition-colors focus-ring"
      onClick={() => navigate(marketListingPath(row))}
      data-testid={`market-row-${row.ref}`}
    >
      {/* Store icons need a signed-in fetch: initials until that exists. */}
      <AppIconTile name={row.name} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 text-[14px] font-semibold text-text-strong">
          <span className="truncate">{row.displayName}</span>
          {row.official && (
            <BadgeCheck size={14} className="text-accent shrink-0" aria-label={t('components.appstore.marketStore.official')} />
          )}
        </div>
        <div className="flex items-center gap-1.5 text-[12px] text-muted min-w-0">
          <span className="truncate min-w-0">{t('components.appstore.marketStore.source_label', { label, publisher: row.publisher })}</span>
          {rating !== null && (
            <span className="inline-flex items-center gap-0.5 shrink-0" title={t('components.appstore.marketStore.rating_label')}>
              <Star size={12} className="shrink-0" aria-hidden /> {fmtNumber(rating, { maximumFractionDigits: 1 })}
            </span>
          )}
          {reviews > 0 && (
            <span className="shrink-0">{t('components.appstore.marketStore.reviews', { n: fmtNumber(reviews) })}</span>
          )}
        </div>
        <div className="text-[12.5px] text-muted truncate" title={row.summary}>{row.summary}</div>
      </div>
      <div
        className="flex flex-col items-end gap-1.5 shrink-0"
        onClick={e => e.stopPropagation()}
        onKeyDown={e => e.stopPropagation()}
        role="presentation"
      >
        {row.installed ? (
          <span className="inline-flex items-center gap-1 text-[12px] text-muted"><Check size={12} /> {t('components.appstore.appListRow.installed')}</span>
        ) : row.nameTaken ? (
          <Btn disabled title={t('components.appstore.marketStore.name_taken')}>{t('components.appstore.appListRow.install')}</Btn>
        ) : (
          <Btn primary className="rounded-full px-3.5 text-[12px] font-semibold" onClick={() => onInstall(row)}>{t('components.appstore.appListRow.install')}</Btn>
        )}
        {row.nameTaken && !row.installed && <span className="text-[12px] text-muted">{t('components.appstore.marketStore.name_taken')}</span>}
      </div>
    </Clickable>
  )
}

export default function MarketListingRows({ offer, query = '' }: { offer: MarketRegistryOffer; query?: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const statusText = useMarketStatusText()
  const [installing, setInstalling] = useState<MarketListingRow | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const { data, error } = useQuery({
    queryKey: marketListingsKey(offer.id),
    queryFn: () => api.listMarketListings(offer.id),
    staleTime: 5 * 60_000,
  })

  const refresh = async () => {
    setRefreshing(true)
    try {
      const fresh = await api.listMarketListings(offer.id, true)
      queryClient.setQueryData<MarketListingsResponse>(marketListingsKey(offer.id), fresh)
    } finally {
      setRefreshing(false)
    }
  }

  const needle = query.trim().toLowerCase()
  const rows = (data?.rows ?? []).filter(r => !needle
    || r.displayName.toLowerCase().includes(needle)
    || r.name.includes(needle)
    || r.summary.toLowerCase().includes(needle))
  const status = data?.status
  const signedOut = !!status && status.state !== 'signed_in'

  return (
    <section className="mb-6" data-testid={`market-section-${offer.id}`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-[17px] font-semibold text-text-strong">{offer.label}</h3>
        <IconButton
          aria-label={t('components.appstore.marketStore.refresh')}
          title={t('components.appstore.marketStore.refresh')}
          onClick={() => { void refresh() }}
          disabled={refreshing}
        >
          <RefreshCw size={14} />
        </IconButton>
      </div>
      {error && <ErrorNotice message={(error as Error).message} askAgent />}
      {data?.error && <ErrorNotice message={t(readErrorKey(data.error.code))} askAgent />}
      {signedOut && (
        <div className="border border-border rounded-xl bg-card px-3.5 py-3 mb-2" data-testid="market-signin-card">
          <div className="text-[14px] font-semibold text-text-strong">{t('components.appstore.marketStore.sign_in_card_title', { label: offer.label })}</div>
          <p className="text-[12.5px] text-muted">{statusText(status)}</p>
          <p className="text-[12.5px] text-muted">{t('components.appstore.marketStore.sign_in_card_body')}</p>
        </div>
      )}
      {!signedOut && data && !data.error && rows.length === 0 && (
        <p className="text-[12.5px] text-muted">{t('components.appstore.marketStore.empty')}</p>
      )}
      {rows.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-3.5">
          {rows.map(row => (
            <MarketRow
              key={`space:${row.registry}:${row.ref}:${cardDataKey(row)}`}
              row={row}
              label={offer.label}
              onInstall={setInstalling}
            />
          ))}
        </div>
      )}
      {data?.truncated && (
        <p className="text-[12px] text-muted mt-1" data-testid="market-truncated">
          {t('components.appstore.marketStore.truncated', { limit: fmtNumber(MARKET_LISTING_LIMIT) })}
        </p>
      )}
      {installing && (
        <InstallFromSpaceDialog
          row={installing}
          onClose={() => setInstalling(null)}
          onInstalled={() => {
            setInstalling(null)
            void queryClient.invalidateQueries({ queryKey: ['market-listings'] })
            window.dispatchEvent(new Event('mc:apps-changed'))
          }}
        />
      )}
    </section>
  )
}

/**
 * Every added Kiro Space store's block, for the Discover page. Renders nothing
 * when the build offers no store or none is added.
 */
export function MarketStoreSections({ query = '' }: { query?: string }) {
  const { data } = useQuery({
    queryKey: MARKET_REGISTRIES_QUERY_KEY,
    queryFn: () => api.getMarketRegistries(),
    staleTime: 30_000,
  })
  const added = new Set(data?.added ?? [])
  const offers = (data?.offers ?? []).filter(o => added.has(o.id))
  if (offers.length === 0) return null
  return <>{offers.map(offer => <MarketListingRows key={offer.id} offer={offer} query={query} />)}</>
}
