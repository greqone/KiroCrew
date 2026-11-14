import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api, type MemberRosterRow } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { RowGroup } from './profileRow'

/** The gateway's bounds (`member_starters.py`); it enforces them, this only words the hint. */
const STARTER_MAX_COUNT = 4
const STARTER_MAX_CHARS = 120

/** One line the gateway left out: its 1-based place in the list sent, and why. */
type LeftOutPlace = { place: number; reason: string }
/** The save's answer: the starters kept, and each line left out. */
type StartersSaved = { warnings?: string[]; starters?: string[]; starters_left_out?: LeftOutPlace[] }
/** A line the last save left out, as typed, with the gateway's reason code. */
type LeftOutLine = { line: string; reason: string }

/** The reasons the editor words; a line left out for any other one is listed without a reason. */
const REASON_KEYS: Record<string, string> = {
  too_long: 'pages.membersPage.profile_starters_reason_too_long',
  repeated: 'pages.membersPage.profile_starters_reason_repeated',
  over_limit: 'pages.membersPage.profile_starters_reason_over_limit',
  credential: 'pages.membersPage.profile_starters_reason_credential',
}

/**
 * The Bot's conversation starters, one per line: the prompts its empty chat offers as
 * buttons. The gateway keeps what passes its bounds and names the place of each line it
 * left out and why, so a line that is too long or repeated never blocks the rest from
 * saving; the box then shows what was kept, and the note under Save says how many were
 * saved and gives each line that went its own reason.
 */
export default function CrewStartersGroup({ member, name }: { member: MemberRosterRow; name: string }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const savedText = (member.starters ?? []).join('\n')
  const [text, setText] = useState(savedText)
  // What the last save kept and left out, in the order typed; null when it left out none.
  const [leftOut, setLeftOut] = useState<{ saved: number; lines: LeftOutLine[] } | null>(null)
  // A change from elsewhere (another window, an applied card) reaches the box; a read
  // that returns the same starters leaves what the owner is typing alone.
  useEffect(() => { setText(savedText) }, [savedText])

  const save = useMutation({
    mutationFn: (starters: string[]) => api.updateKirocrewAgent(member.name, { starters }) as Promise<StartersSaved>,
    onSuccess: (result, sent) => {
      // The box shows what was saved, even when that equals the starters it already had.
      const kept = Array.isArray(result?.starters) ? result.starters : null
      if (kept) setText(kept.join('\n'))
      const lines = (result?.starters_left_out ?? []).flatMap(({ place, reason }) => {
        const line = sent[place - 1]
        return typeof line === 'string' ? [{ line, reason }] : []
      })
      setLeftOut(lines.length > 0 ? { saved: kept?.length ?? 0, lines } : null)
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }),
  })
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean)
  const dirty = lines.join('\n') !== savedText

  return (
    <>
      <RowGroup caption={t('pages.membersPage.profile_starters_hint', { name, max: STARTER_MAX_COUNT, chars: STARTER_MAX_CHARS })}>
        <div className="flex flex-col gap-2 px-3.5 py-3" data-testid="crew-profile-starters">
          <label className="text-[13px] font-semibold text-text" htmlFor="crew-profile-starters-input">
            {t('pages.membersPage.profile_starters_title')}
          </label>
          <textarea
            id="crew-profile-starters-input"
            aria-label={t('pages.membersPage.profile_starters_title')}
            value={text}
            rows={4}
            disabled={save.isPending}
            onChange={(e) => { setText(e.target.value); setLeftOut(null) }}
            className="w-full rounded-lg border border-border bg-bg px-2.5 py-2 text-[13px] text-text focus-ring"
            data-testid="crew-profile-starters-input"
          />
          <div className="flex justify-end">
            <Btn
              type="button"
              primary
              disabled={!dirty || save.isPending}
              onClick={() => save.mutate(lines)}
              data-testid="crew-profile-starters-save"
            >
              {t('pages.membersPage.profile_starters_save')}
            </Btn>
          </div>
          {leftOut && (
            <div className="text-[12.5px] leading-snug text-warn-text" role="status" data-testid="crew-profile-starters-left-out">
              <p>{t('pages.membersPage.profile_starters_left_out', { count: leftOut.saved })}</p>
              <ul className="mt-1 list-disc pl-5">
                {leftOut.lines.map(({ line, reason }, index) => (
                  <li key={index} className="break-words" data-testid="crew-profile-starters-left-out-line">
                    {REASON_KEYS[reason]
                      ? t(REASON_KEYS[reason], { max: STARTER_MAX_COUNT, chars: STARTER_MAX_CHARS, line })
                      : line}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </RowGroup>
      {/* No hand-off: the starter draft in the box above is not saved when Save
          fails, so a navigation to the chat would discard it. */}
      <ErrorNotice message={save.isError ? t('pages.membersPage.profile_starters_failed') : null} testId="crew-profile-starters-error" />
    </>
  )
}
