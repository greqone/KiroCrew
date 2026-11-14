/**
 * Kiro Space stores as App Store sources (the S2b surfaces).
 *
 * - The Sources block and the Discover sections are absent when the build offers
 *   no store (the open-source build has no provider).
 * - A signed-out store shows a sign-in card; `no_access` offers both Sign out and
 *   Sign in.
 * - A store row and a git row of the same name are two cards with different keys,
 *   and the store row installs by ref through `/api/apps/market/install`, never the
 *   by-name registry install. Opening it goes to `/apps/-/market/...`.
 * - `truncated` shows the "first 1,000" note; confirming the dialog calls confirm.
 * - A git row whose installed app came from a store says so and has no Update.
 * - A `market:` installed app has no Sync or Update anywhere it would render one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import '../i18n/all'

const api = vi.hoisted(() => ({
  getMarketRegistries: vi.fn(),
  setMarketRegistries: vi.fn(),
  marketSignIn: vi.fn(),
  marketSignOut: vi.fn(),
  listMarketListings: vi.fn(),
  getMarketListing: vi.fn(),
  prepareMarketInstall: vi.fn(),
  confirmMarketInstall: vi.fn(),
  installFromRegistry: vi.fn(),
  installFromRegistryStream: vi.fn(),
  updateApp: vi.fn(),
}))
vi.mock('../api/client', () => ({ api }))

import MarketRegistryManager from '../components/appstore/MarketRegistryManager'
import { MarketStoreSections, marketListingPath } from '../components/appstore/MarketListingRows'
import AppListRow from '../components/appstore/AppListRow'
import { isMarketSourced, type MarketListingRow, type RegistryApp } from '../components/appstore/types'

const OFFER = { id: 'team-store', label: 'Team store', web: 'https://store.example' }

function row(extra: Partial<MarketListingRow> = {}): MarketListingRow {
  return {
    registry: 'team-store', ref: 'acme/board', name: 'board', displayName: 'Board',
    summary: 'A board', category: 'productivity', tags: [], publisher: 'acme',
    latestVersion: 2, latestSemver: '1.1.0', trust: 'COMMUNITY', official: false, canPublish: false,
    rating: { status: 'absent' }, ratingCount: { status: 'ok', value: 0 }, downloads: { status: 'absent' },
    installed: false, installedVersion: null, nameTaken: false, ...extra,
  }
}

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname}</div>
}

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/apps']}>
        <Routes>
          <Route path="/apps" element={<>{node}<Where /></>} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset()
})

describe('with no store offered', () => {
  it('draws neither the Sources block nor a Discover section', async () => {
    api.getMarketRegistries.mockResolvedValue({ offers: [], added: [], status: {} })
    const { container } = wrap(<><MarketRegistryManager /><MarketStoreSections /></>)
    await waitFor(() => expect(api.getMarketRegistries).toHaveBeenCalled())
    expect(screen.queryByTestId('market-registry-manager')).toBeNull()
    expect(container.querySelector('[data-testid^="market-section-"]')).toBeNull()
    expect(api.listMarketListings).not.toHaveBeenCalled()
  })
})

describe('the Sources block', () => {
  it('offers Add for a store that is not added, and adds it by id', async () => {
    api.getMarketRegistries.mockResolvedValue({ offers: [OFFER], added: [], status: {} })
    api.setMarketRegistries.mockResolvedValue({ ok: true, added: ['team-store'] })
    wrap(<MarketRegistryManager />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add' }))
    await waitFor(() => expect(api.setMarketRegistries).toHaveBeenCalledWith(['team-store']))
  })

  it('shows Sign out and Sign in for no_access', async () => {
    api.getMarketRegistries.mockResolvedValue({
      offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'no_access', account: '' } },
    })
    wrap(<MarketRegistryManager />)
    expect(await screen.findByText('Kiro Space refused this sign-in.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
  })

  it('shows the code and the approval page after Sign in', async () => {
    api.getMarketRegistries.mockResolvedValue({
      offers: [OFFER], added: ['team-store'], status: { 'team-store': { state: 'needs_sign_in', account: '' } },
    })
    api.marketSignIn.mockResolvedValue({ ok: true, registry: 'team-store', state: 'pending', code: 'WXYZ', approvalPage: 'https://store.example/approve' })
    wrap(<MarketRegistryManager />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText('WXYZ')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /approval page/i })).toHaveAttribute('href', 'https://store.example/approve')
    expect(api.marketSignIn).toHaveBeenCalledWith('team-store')
  })
})

describe('the Discover section', () => {
  const added = { offers: [OFFER], added: ['team-store'], status: {} }

  it('draws a sign-in card for a signed-out store', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [], truncated: false, status: { state: 'needs_sign_in', account: '' } })
    wrap(<MarketStoreSections />)
    expect(await screen.findByTestId('market-signin-card')).toBeInTheDocument()
    expect(screen.getByText('Sign in to Team store')).toBeInTheDocument()
  })

  it('installs by ref through the market route, not the by-name registry install', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [row()], truncated: false, status: { state: 'signed_in', account: 'a' } })
    api.prepareMarketInstall.mockResolvedValue({
      ok: true, stagingId: 'S'.repeat(22),
      confirm: {
        app: { ref: 'acme/board', version: 2, appVersion: '1.1.0', publisher: 'acme', sha256Prefix: 'abcdef012345' },
        components: [], size: 2048, trustLabel: 'Community, not reviewed by Kiro Space',
        permissions: { notice: 'Declared by the publisher, not enforced.', commands: [], network: ['api.example'], files: [], background: [], mcpServers: [], agents: [] },
      },
    })
    api.confirmMarketInstall.mockResolvedValue({ ok: true, name: 'board' })
    wrap(<MarketStoreSections />)
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }))
    expect(await screen.findByTestId('market-install-confirm')).toBeInTheDocument()
    expect(api.prepareMarketInstall).toHaveBeenCalledWith('acme/board')
    expect(screen.getByText('abcdef012345')).toBeInTheDocument()
    expect(screen.getByText('api.example')).toBeInTheDocument()
    const dialogInstall = screen.getAllByRole('button', { name: 'Install' }).at(-1)!
    fireEvent.click(dialogInstall)
    await waitFor(() => expect(api.confirmMarketInstall).toHaveBeenCalledWith('S'.repeat(22), true))
    expect(api.installFromRegistry).not.toHaveBeenCalled()
    expect(api.installFromRegistryStream).not.toHaveBeenCalled()
  })

  it('opens the listing at /apps/-/market/...', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [row()], truncated: false, status: { state: 'signed_in', account: 'a' } })
    wrap(<MarketStoreSections />)
    fireEvent.click(await screen.findByTestId('market-row-acme/board'))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/apps/-/market/team-store/acme/board'))
    expect(marketListingPath(row())).toBe('/apps/-/market/team-store/acme/board')
  })

  it('says only the first 1,000 show when the list is truncated', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [row()], truncated: true, status: { state: 'signed_in', account: 'a' } })
    wrap(<MarketStoreSections />)
    expect(await screen.findByTestId('market-truncated')).toHaveTextContent('Showing only the first 1,000 apps.')
  })

  it('keeps a same-name store row and git row as two cards with their own labels', async () => {
    api.getMarketRegistries.mockResolvedValue(added)
    api.listMarketListings.mockResolvedValue({ ok: true, registry: 'team-store', rows: [row({ nameTaken: true })], truncated: false, status: { state: 'signed_in', account: 'a' } })
    const git = { name: 'board', displayName: 'Board', description: '', version: '1.0.0', author: 'Kiro Crew', tags: [], installed: true } as RegistryApp
    const noop = () => {}
    wrap(<><MarketStoreSections /><AppListRow connected app={git} onOpen={noop} onGet={noop} onUpdate={noop} onEnable={noop} /></>)
    const storeRow = await screen.findByTestId('market-row-acme/board')
    expect(storeRow).toHaveTextContent('Team store · acme')
    expect(storeRow).toHaveTextContent('An app with this name is already installed from another source.')
    expect(screen.getAllByText('Board')).toHaveLength(2)
  })
})

describe('a git row over a Kiro Space install', () => {
  it('says it came from Kiro Space and offers no Update', () => {
    const noop = () => {}
    const app = { name: 'board', displayName: 'Board', description: '', version: '9.0.0', author: 'x', tags: [], installed: true, updateAvailable: true, installedFrom: 'market' } as RegistryApp
    render(<AppListRow connected app={app} onOpen={noop} onGet={noop} onUpdate={noop} onEnable={noop} />)
    expect(screen.getByTestId('installed-from-market')).toHaveTextContent('Installed from Kiro Space')
    expect(screen.queryByRole('button', { name: /update/i })).toBeNull()
  })
})

describe('isMarketSourced', () => {
  it('is true only for a market: source string', () => {
    expect(isMarketSourced({ source: 'market:acme/board@2' })).toBe(true)
    expect(isMarketSourced({ source: 'registry:board' })).toBe(false)
    expect(isMarketSourced({ source: { type: 'git' } as unknown as string })).toBe(false)
  })
})

describe('a Kiro Space install in the Library', () => {
  it('has no Sync in its tile menu', async () => {
    const { default: LaunchpadTile } = await import('../pages/apps/LaunchpadTile')
    const { act } = await import('@testing-library/react')
    vi.useFakeTimers()
    try {
      const app = {
        name: 'board', displayName: 'Board', version: '1.0.0', enabled: true, lifecycle: 'gateway',
        updateAvailable: false, manifest: {}, source: 'market:acme/board@2',
      } as unknown as import('../pages/apps/useAppsData').LibraryApp
      render(
        <LaunchpadTile app={app} pinned connected actionLoading={null} onTogglePin={() => {}}
          onAction={() => {}} onOpen={() => {}} onDetail={() => {}} />,
      )
      const trigger = screen.getByRole('button', { name: 'More actions for Board' })
      trigger.focus()
      fireEvent.keyDown(trigger, { key: 'Enter' })
      act(() => { vi.runAllTimers() })
      expect(screen.getByRole('menuitem', { name: 'Details' })).toBeInTheDocument()
      expect(screen.queryByRole('menuitem', { name: 'Sync' })).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('is skipped by a row update and by Update All', async () => {
    const { renderHook } = await import('@testing-library/react')
    const { useAppUpdates } = await import('../pages/apps/useAppUpdates')
    const updateApp = vi.fn()
    const market = { name: 'board', displayName: 'Board', source: 'market:acme/board@2' }
    const { result } = renderHook(() => useAppUpdates({
      apps: [market] as never, updatables: [market] as never, announceAppsChanged: () => {},
      updateApp, setError: () => {}, setSuccess: () => {},
    } as never))
    await result.current.runUpdate('board')
    await result.current.updateAll()
    expect(updateApp).not.toHaveBeenCalled()
    expect(api.updateApp).not.toHaveBeenCalled()
  })
})
