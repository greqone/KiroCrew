/**
 * Safety switches (Settings -> Security): the owner's toggles for
 * `mcp.protect_kiro_permission_rules` and `mcp.honour_auto_approve`.
 *
 * The card is a VIEW over `/api/security/safety-switches`; the backend's rules
 * are in test/test_safety_switches_routes.py. Pinned here: both toggles draw the
 * stored values, a click sends exactly that switch's key, a non-owner sees them
 * disabled, a degraded read says so, and a failed read draws no switch.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { SafetySwitchesState } from '../../api/client'
import { ApiError } from '../../api/apiError'

vi.mock('../../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../../api/apiError')>('../../api/apiError')).ApiError,
  api: {
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    safetySwitches: vi.fn(),
    setSafetySwitch: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

const mock = (fn: unknown) => fn as ReturnType<typeof vi.fn>

function state(protect: boolean, honour: boolean, degraded = false, protectInForce = protect): SafetySwitchesState {
  return {
    degraded,
    switches: [
      { key: 'mcp.protect_kiro_permission_rules', enabled: protect, in_force: protectInForce },
      { key: 'mcp.honour_auto_approve', enabled: honour, in_force: honour },
    ],
  }
}

function render() {
  return renderWithProviders(<SecurityPanel />, { route: '/?section=safety', queryDefaults: { retryDelay: 0 } })
}

const protectSwitch = () => within(screen.getByTestId('safety-switch-protect')).getByRole('switch')
const honourSwitch = () => within(screen.getByTestId('safety-switch-honour')).getByRole('switch')

describe('SecurityPanel - safety switches', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    mock(api.deniedCommands).mockResolvedValue({ builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false })
    mock(api.securityPosture).mockResolvedValue({ controls: [], counts: {} })
    mock(api.governancePolicy).mockResolvedValue({ version: null, has_policy: false, profile: null, unavailable: false, scopes: [] })
    mock(api.kirocrewConfig).mockResolvedValue({})
    mock(api.tailnetStatus).mockResolvedValue({ enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off' })
  })

  it('draws both toggles with the stored values', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(true, false))
    render()
    await screen.findByTestId('safety-switches-rows')
    expect(protectSwitch()).toHaveAttribute('aria-checked', 'true')
    // The ask toggle is the INVERSE of mcp.honour_auto_approve, so on is safer for both.
    expect(honourSwitch()).toHaveAttribute('aria-checked', 'true')
    expect(protectSwitch()).not.toHaveAttribute('aria-disabled')
    expect(screen.queryByTestId('safety-switches-degraded')).toBeNull()
  })

  it('a click sends exactly that switch key and the new value', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(false, true))
    mock(api.setSafetySwitch).mockResolvedValue(state(true, true))
    render()
    await screen.findByTestId('safety-switches-rows')
    mock(api.safetySwitches).mockResolvedValue(state(true, true))
    fireEvent.click(protectSwitch())
    await waitFor(() => expect(api.setSafetySwitch).toHaveBeenCalledWith('mcp.protect_kiro_permission_rules', true))
    await waitFor(() => expect(protectSwitch()).toHaveAttribute('aria-checked', 'true'))

    // honour is on (true), so the ask toggle reads off; turning it ON sends honour=false.
    expect(honourSwitch()).toHaveAttribute('aria-checked', 'false')
    mock(api.setSafetySwitch).mockResolvedValue(state(true, false))
    mock(api.safetySwitches).mockResolvedValue(state(true, false))
    fireEvent.click(honourSwitch())
    await waitFor(() => expect(api.setSafetySwitch).toHaveBeenLastCalledWith('mcp.honour_auto_approve', false))
    expect(api.setSafetySwitch).toHaveBeenCalledTimes(2)
    await waitFor(() => expect(honourSwitch()).toHaveAttribute('aria-checked', 'true'))
    // honour applies when the agent spec is rebuilt, so the flip says to restart.
    expect(await screen.findByTestId('safety-switch-restart-hint')).toBeTruthy()
  })

  it('a non-owner sees both toggles disabled, from the config they can read, with the owner-only notice', async () => {
    mock(api.safetySwitches).mockRejectedValue(new ApiError(403, 'dashboard owner required', JSON.stringify({ code: 'dashboard_owner_required' })))
    mock(api.kirocrewConfig).mockResolvedValue({ mcp: { protect_kiro_permission_rules: false, honour_auto_approve: true } })
    render()
    await screen.findByTestId('safety-switches-owner-only')
    await screen.findByTestId('safety-switch-protect')
    expect(protectSwitch()).toHaveAttribute('aria-disabled', 'true')
    expect(honourSwitch()).toHaveAttribute('aria-disabled', 'true')
    expect(protectSwitch()).toHaveAttribute('aria-checked', 'false')
    // honour_auto_approve true in config -> the ask toggle reads off.
    expect(honourSwitch()).toHaveAttribute('aria-checked', 'false')
    // A permission lock, not an error: neutral, no ask-agent hand-off.
    expect(screen.queryByText('Open a chat with the agent about this error')).toBeNull()
    expect(within(screen.getByTestId('safety-switches-rows')).queryByRole('alert')).toBeNull()
    // A refusal is a fact about the viewer: read once, never retried.
    expect(api.safetySwitches).toHaveBeenCalledTimes(1)
    fireEvent.click(protectSwitch())
    expect(api.setSafetySwitch).not.toHaveBeenCalled()
  })

  it('a degraded read says so and shows what is in force', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(false, true, true, true))
    render()
    await screen.findByTestId('safety-switches-degraded')
    expect(screen.getByText('On right now, because the settings file could not be read in full.')).toBeTruthy()
  })

  it('the in-force note follows the shown (inverted) ask toggle', async () => {
    // honour configured on, but in force off: the ask toggle shows off, and ON is in force.
    mock(api.safetySwitches).mockResolvedValue({
      degraded: true,
      switches: [
        { key: 'mcp.protect_kiro_permission_rules', enabled: true, in_force: true },
        { key: 'mcp.honour_auto_approve', enabled: true, in_force: false },
      ],
    })
    render()
    await screen.findByTestId('safety-switches-degraded')
    expect(honourSwitch()).toHaveAttribute('aria-checked', 'false')
    expect(within(screen.getByTestId('safety-switch-honour')).getByText('On right now, because the settings file could not be read in full.')).toBeTruthy()
  })

  it('flipping protect, which applies live, shows no restart hint', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(false, true))
    mock(api.setSafetySwitch).mockResolvedValue(state(true, true))
    render()
    await screen.findByTestId('safety-switches-rows')
    mock(api.safetySwitches).mockResolvedValue(state(true, true))
    fireEvent.click(protectSwitch())
    await waitFor(() => expect(protectSwitch()).toHaveAttribute('aria-checked', 'true'))
    expect(screen.queryByTestId('safety-switch-restart-hint')).toBeNull()
  })

  it('a failed read draws no switch and offers Retry', async () => {
    mock(api.safetySwitches).mockRejectedValue(new Error('boom'))
    render()
    await screen.findByTestId('safety-switches-read-failed')
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.getByTestId('safety-switches-retry')).toBeTruthy()
  })

  it('a refused write keeps the position and says so', async () => {
    mock(api.safetySwitches).mockResolvedValue(state(false, true))
    mock(api.setSafetySwitch).mockRejectedValue(new ApiError(503, 'audit unavailable', JSON.stringify({ code: 'safety_switch_audit_unavailable' })))
    render()
    await screen.findByTestId('safety-switches-rows')
    fireEvent.click(protectSwitch())
    await screen.findByTestId('safety-switches-write-failed')
    expect(protectSwitch()).toHaveAttribute('aria-checked', 'false')
  })
})
