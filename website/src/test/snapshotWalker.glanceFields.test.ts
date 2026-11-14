/**
 * The walker's `glance` (`src/kiro_crew/browser_harness/snapshot_walker.js`), the
 * page read behind a browser action's `changes` report, run against every kind of
 * field inside every text it reads.
 *
 * What a field holds is a value, whether a person typed it or the page put it
 * there, so `glance` reads nothing inside a field: not in an alert, a wrapping
 * label or a dialog heading, and not from an element that sits inside a field. An
 * invalid field is named, and its validation message is never read, since a
 * message can repeat the value in any form. The controls scan and every text read use one test of
 * "this is a field". Each case puts the same marker in the field and checks that
 * no list `glance` returns repeats it.
 */
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { readSource } from './readSource'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readSource(join(HARNESS_DIR, 'snapshot_walker.js')).split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

interface Glance {
  controls: string[]
  dialogs: string[]
  alerts: string[]
  invalid: string[]
}

const glance = (): Glance =>
  (new Function(`return (${walkerSource()})`)() as { glance: () => Glance }).glance()

const CODE = '482913'

// Each kind of field, holding the marker as its value or as its own text.
const KINDS: Record<string, string> = {
  'an input': `<input name="f" value="${CODE}">`,
  'a textarea': `<textarea name="f">${CODE}</textarea>`,
  'a select': `<select name="f"><option>${CODE}</option></select>`,
  'an editable region': `<div contenteditable="true">${CODE}</div>`,
  'a role textbox': `<div role="textbox" id="otp">${CODE}</div>`,
  'a role searchbox': `<div role="searchbox">${CODE}</div>`,
  'a role combobox': `<div role="combobox">${CODE}</div>`,
  'a role spinbutton': `<div role="spinbutton">${CODE}</div>`,
  'a role slider': `<div role="slider">${CODE}</div>`,
  'a role with a second token': `<div role="textbox custom">${CODE}</div>`,
  'a custom element with a role': `<x-code role="textbox">${CODE}</x-code>`,
  'a shadow host holding a field': `<x-field>${CODE}</x-field>`,
}

// Each text `glance` reads, wrapped around one field.
const CONTAINERS: Record<string, (field: string) => string> = {
  alert: (k) => `<div role="alert">Code sent ${k}</div>`,
  label: (k) => `<label>Code ${k}<input name="x" aria-invalid="true"></label>`,
  'dialog heading': (k) => `<div role="dialog"><h2>Verify ${k}</h2><button>OK</button></div>`,
  'aria-describedby message': (k) =>
    `<input name="x" aria-invalid="true" aria-describedby="m"><div id="m">Wrong ${k}</div>`,
  'aria-errormessage message': (k) =>
    `<input name="x" aria-invalid="true" aria-errormessage="m"><div id="m">Wrong ${k}</div>`,
}

function render(html: string): void {
  const parsed = new DOMParser().parseFromString(`<!doctype html><body>${html}</body>`, 'text/html')
  document.body.replaceChildren(...Array.from(parsed.body.childNodes))
  for (const host of document.querySelectorAll('x-field')) {
    const inner = document.createElement('input')
    inner.name = 'inner'
    host.attachShadow({ mode: 'open' }).replaceChildren(inner, document.createElement('slot'))
  }
}

const said = (g: Glance): string => JSON.stringify([g.controls, g.dialogs, g.alerts, g.invalid])

beforeEach(() => {
  // The test DOM lays nothing out: give every element a box so `visible()` sees it.
  Element.prototype.getBoundingClientRect = function () {
    return { x: 0, y: 0, left: 0, top: 0, width: 100, height: 20, right: 100, bottom: 20, toJSON: () => ({}) } as DOMRect
  }
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', { configurable: true, get: () => document.body })
  // An editable region, as a browser computes it: the nearest `contenteditable` that is not "false".
  Object.defineProperty(HTMLElement.prototype, 'isContentEditable', {
    configurable: true,
    get(this: HTMLElement) {
      const owner = this.closest('[contenteditable]')
      return !!owner && owner.getAttribute('contenteditable') !== 'false'
    },
  })
})

afterEach(() => {
  document.body.replaceChildren()
})

describe('glance reads nothing a field holds', () => {
  it('a one-time code in a custom role="textbox" inside an alert stays out of the report', () => {
    render(`<div role="alert">Your code <div role="textbox" id="otp">${CODE}</div> was sent</div>`)
    const g = glance()
    expect(said(g)).not.toContain(CODE)
    expect(g.alerts).toEqual(['Your code was sent'])
  })

  for (const [container, wrap] of Object.entries(CONTAINERS)) {
    for (const [kind, field] of Object.entries(KINDS)) {
      it(`${kind} inside the ${container}`, () => {
        render(wrap(field))
        expect(said(glance())).not.toContain(CODE)
      })
    }
  }

  it('a message whose element sits inside a field reads as nothing', () => {
    render(
      `<div role="textbox"><span id="m">${CODE}</span></div>` +
        `<input name="x" aria-invalid="true" aria-describedby="m">`,
    )
    const g = glance()
    expect(said(g)).not.toContain(CODE)
    expect(g.invalid).toEqual(['x'])
  })

  it('an alert inside an editable region reads as nothing', () => {
    render(`<div contenteditable="true"><div role="alert">${CODE}</div></div>`)
    expect(glance().alerts).toEqual([])
  })
})

describe('glance still reads the page text around the fields', () => {
  it('alert, dialog and label text without a field are reported', () => {
    render(
      `<div role="alert">Saved <b>2</b> items</div>` +
        `<div role="dialog"><h2>Confirm order</h2></div>` +
        `<label>Email <input type="email" name="e" aria-invalid="true" aria-describedby="h"></label>` +
        `<div id="h">Use your work address</div><button>Pay now</button>`,
    )
    const g = glance()
    expect(g.alerts).toEqual(['Saved 2 items'])
    expect(g.dialogs).toEqual(['Confirm order'])
    expect(g.invalid).toEqual(['Email'])
    expect(g.controls).toEqual(['textbox|Email', 'button|Pay now'])
  })
})

describe('glance names an invalid field and never quotes its message', () => {
  it('a describedby message that repeats the value with doubled spaces stays out', () => {
    const typed = 'ref  77  4410'
    render(
      `<label>Reference <input name="ref" aria-invalid="true" aria-describedby="m"></label>` +
        `<div id="m">"${typed}" is not a known reference</div>`,
    )
    const input = document.querySelector('input') as HTMLInputElement
    input.value = typed
    const g = glance()
    expect(g.invalid).toEqual(['Reference'])
    expect(said(g)).not.toContain('4410')
  })

  it("an email field's own validation message that quotes its value stays out", () => {
    render(`<label>Email <input type="email" name="e" aria-invalid="true"></label>`)
    const input = document.querySelector('input') as HTMLInputElement
    input.value = 'abc'
    Object.defineProperty(input, 'validationMessage', {
      configurable: true,
      get: () => `Please include an '@' in the email address. 'abc' is missing an '@'.`,
    })
    const g = glance()
    expect(g.invalid).toEqual(['Email'])
    expect(said(g)).not.toContain('abc')
  })
})
