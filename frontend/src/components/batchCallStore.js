// batchCallStore.js — v7: adds concurrency (wave-based) + pre-flight
// validation on top of v6's durable-campaign backend.
//
// CONCURRENCY MODEL — wave-based, not a sliding worker pool: pick up to
// `concurrency` PENDING rows, dial them all in parallel, wait for the
// WHOLE wave to finish, then pick the next wave. Matches "batch of 3/5
// go, then next batch" rather than "always keep 3 in flight". Each
// row's own arrow (activeIndexes) clears the moment THAT row's call
// resolves — it does not wait for the rest of its wave.
//
// PRE-FLIGHT — validateRows() checks E.164 validity + duplicate numbers
// before Start actually dials anything; UI shows counts and needs a
// second confirm click if there are invalid/duplicate rows.
import * as XLSX from 'xlsx'
import { useState, useEffect } from 'react'
import { supabase } from '../supabaseClient'

export const COUNTRY_CODES = [
  { code: '91',  label: '🇮🇳 +91 India' },
  { code: '1',   label: '🇺🇸 +1 USA/Canada' },
  { code: '44',  label: '🇬🇧 +44 UK' },
  { code: '61',  label: '🇦🇺 +61 Australia' },
  { code: '971', label: '🇦🇪 +971 UAE' },
  { code: '65',  label: '🇸🇬 +65 Singapore' },
]

export function toE164(rawValue, defaultCountryCode) {
  let v = String(rawValue ?? '').trim()
  if (!v) return ''
  const hadPlus = v.startsWith('+')
  let digits = v.replace(/\D/g, '')
  if (!digits) return ''
  if (hadPlus) return '+' + digits
  if (defaultCountryCode && digits.startsWith(defaultCountryCode) && digits.length > 10) return '+' + digits
  // BUGFIX: domestic trunk-prefix leading zero (Indian "09876543210",
  // UK "07911123456") must be stripped BEFORE prepending the country
  // code — otherwise "09876543210" + cc "91" produced "+9109876543210"
  // instead of "+919876543210". Only strips leading zeros; digits.length
  // requirement above already handles numbers that already carry a cc.
  if (digits.startsWith('0')) digits = digits.replace(/^0+/, '')
  return '+' + defaultCountryCode + digits
}

// E.164: + followed by 8-15 digits total (ITU max length), first digit 1-9.
export function isValidE164(v) { return /^\+[1-9]\d{7,14}$/.test(v) }

const PHONE_HEADER_CANDIDATES = ['phone', 'phone number', 'number', 'mobile', 'contact', 'to', 'phone_number']
const NAME_HEADER_CANDIDATES  = ['name', 'lead name', 'customer name', 'contact name']

export const BATCH_STATUSES = { PENDING: 'Pending', FAILED: 'Failed', UNKNOWN: 'Unknown' }
// Mirrors campaigns.py's NON_TERMINAL set. A row sitting in any of these
// business_statuses has NOT actually finished — it just hasn't been
// re-labeled "Pending" locally (see BUGFIX below).
const NON_TERMINAL_STATUSES = new Set(['Pending', 'QUEUED', 'DIALING', 'RINGING', 'IN_PROGRESS'])
export const CONCURRENCY_OPTIONS = [1, 3]

const state = {
  countryCode: '91', agentId: '', fileName: '', fileType: '',
  columns: [], phoneKey: '', nameKey: '',
  campaignId: null, hasStarted: false, starting: false,
  concurrency: 3,
  rows: [],                    // [{ __row, __phone, __status, __call_uuid, __lead_id, ...original }]
  activeIndexes: new Set(),    // NEW — rows currently mid-call, replaces single `index`
  running: false, currentLog: null,
  fileHandle: null, canLiveWrite: false, sheetLinkUrl: '',
  preflight: null,             // { total, validCount, invalidRows:[idx], duplicateRows:[idx] }
}
const control = { stopRequested: false, pause: false, loopAlive: false, reconcileTimer: null }
// `starting` lives on `state` (not `control`) so the UI can read
// batch.starting and disable the Start button while it's true — but it
// is still set SYNCHRONOUSLY, before any `await`, in startBatch() below.
// That's what actually prevents the double-click race: disabling the
// button is a courtesy for the normal case, the synchronous flag is
// what protects against a second click landing in the same JS
// microtask gap before React re-renders the disabled button.
const listeners = new Set()
function notify() { const s = { ...state }; listeners.forEach(cb => cb(s)) }

export function useBatchStore() {
  const [snap, setSnap] = useState({ ...state })
  useEffect(() => {
    listeners.add(setSnap)
    setSnap({ ...state })
    return () => listeners.delete(setSnap)
  }, [])
  return snap
}

export function setCountryCode(v) { state.countryCode = v; state.preflight = null; notify() }
export function setAgentId(v) { state.agentId = v; notify() }
export function setConcurrency(n) { state.concurrency = Math.max(1, Math.min(3, Number(n) || 1)); notify() }

export function loadFromFileInput(file) {
  state.fileName = file.name
  state.fileType = file.name.toLowerCase().endsWith('.csv') ? 'csv' : 'xlsx'
  state.fileHandle = null; state.canLiveWrite = false; state.sheetLinkUrl = ''
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = (evt) => { try { parseWorkbookBinary(evt.target.result); resolve() } catch (err) { reject(err) } }
    reader.onerror = reject
    reader.readAsBinaryString(file)
  })
}

export async function loadFromFilePicker() {
  if (!window.showOpenFilePicker) throw new Error('Live sheet updates need Chrome or Edge.')
  const [handle] = await window.showOpenFilePicker({
    types: [{ description: 'Spreadsheet', accept: { 'text/csv': ['.csv'], 'application/vnd.ms-excel': ['.xls', '.xlsx'] } }],
  })
  const perm = await handle.requestPermission({ mode: 'readwrite' })
  if (perm !== 'granted') throw new Error('Write permission denied.')
  const file = await handle.getFile()
  state.fileName = file.name
  state.fileType = file.name.toLowerCase().endsWith('.csv') ? 'csv' : 'xlsx'
  state.fileHandle = handle; state.canLiveWrite = true; state.sheetLinkUrl = ''
  parseWorkbookBinary(await file.arrayBuffer())
}

function parseWorkbookBinary(binaryOrBuffer) {
  const wb = XLSX.read(binaryOrBuffer, { type: typeof binaryOrBuffer === 'string' ? 'binary' : 'array' })
  const sheet = wb.Sheets[wb.SheetNames[0]]
  const json = XLSX.utils.sheet_to_json(sheet, { defval: '' })
  if (!json.length) throw new Error('Sheet is empty')
  const cols = Object.keys(json[0])
  const phoneGuess = cols.find(c => PHONE_HEADER_CANDIDATES.includes(c.trim().toLowerCase())) || cols[0]
  const nameGuess  = cols.find(c => NAME_HEADER_CANDIDATES.includes(c.trim().toLowerCase())) || ''

  state.columns = cols; state.phoneKey = phoneGuess; state.nameKey = nameGuess
  state.rows = json.map((row, i) => ({
    __row: i + 1, __phone: String(row[phoneGuess] ?? ''),
    __status: BATCH_STATUSES.PENDING, __call_uuid: '', __lead_id: null,
    ...row,
  }))
  state.activeIndexes = new Set(); state.currentLog = null
  state.campaignId = null; state.hasStarted = false; state.preflight = null
  notify()
}

// BUGFIX (Google Sheets URL handling) — the UI's own label says "shared
// or published-to-web", but this used to hard-require output=csv or
// /pub, so a normal Share link (the ...d/<ID>/edit?usp=sharing format
// almost everyone actually copies) always failed despite the UI
// claiming to support it. Now a normal share link is converted to
// Sheets' CSV export endpoint, which works for anything shared as
// "Anyone with the link can view" — no "Publish to web" step required.
// Already-published (/pub, output=csv) and already-export links are
// left untouched and still work exactly as before.
function toGoogleSheetsCsvExportUrl(rawUrl) {
  const url = rawUrl.trim()
  if (url.includes('output=csv') || url.includes('/export')) return url
  const m = url.match(/\/spreadsheets\/d\/([a-zA-Z0-9-_]+)/)
  if (!m) throw new Error('Not a recognizable Google Sheets link — paste the link from the address bar or Share dialog.')
  const gidMatch = url.match(/[?#&]gid=(\d+)/)
  const gid = gidMatch ? gidMatch[1] : '0'
  return `https://docs.google.com/spreadsheets/d/${m[1]}/export?format=csv&gid=${gid}`
}

export async function loadFromGoogleSheetCsvUrl(url) {
  const csvUrl = toGoogleSheetsCsvExportUrl(url)
  const res = await fetch(csvUrl)
  if (!res.ok) throw new Error('Could not fetch the sheet — make sure sharing is set to "Anyone with the link can view".')
  const text = await res.text()
  // A private/restricted sheet returns 200 with an HTML sign-in page,
  // not a fetch error — catch that explicitly instead of feeding HTML
  // into the CSV/XLSX parser, which would fail with a confusing
  // "Sheet is empty" error that doesn't point at the real cause.
  if (/^\s*<(!doctype|html)/i.test(text)) {
    throw new Error('Sheet isn\'t publicly viewable — set sharing to "Anyone with the link can view" and try again.')
  }
  state.fileName = 'google_sheet_import.csv'; state.fileType = 'csv'
  state.fileHandle = null; state.canLiveWrite = false; state.sheetLinkUrl = url
  parseWorkbookBinary(text)
}

// ── pre-flight validation — call before Start. Does not mutate row
// status, only computes counts so the UI can warn + require a second
// confirm click when there are invalid/duplicate numbers.
export function validateRows() {
  const seen = new Map()
  const invalidRows = [], duplicateRows = []
  state.rows.forEach((r, i) => {
    const e164 = toE164(r.__phone, state.countryCode)
    if (!r.__phone.trim() || !isValidE164(e164)) { invalidRows.push(i); return }
    if (seen.has(e164)) duplicateRows.push(i)
    else seen.set(e164, i)
  })
  state.preflight = {
    total: state.rows.length,
    validCount: state.rows.length - invalidRows.length - duplicateRows.length,
    invalidRows, duplicateRows,
  }
  notify()
  return state.preflight
}

function rowsWithStatusColumn() {
  return state.rows.map(r => {
    const { __row, __phone, __call_uuid, __status, __lead_id, ...original } = r
    const out = {}
    for (const key of state.columns) {
      out[key] = original[key]
      if (key === state.phoneKey) out['Call Status'] = __status
    }
    if (!(state.phoneKey in out)) out['Call Status'] = __status
    return out
  })
}

function updateRow(rowIdx, patch) { state.rows = state.rows.map((r, i) => i === rowIdx ? { ...r, ...patch } : r); notify() }
function setCurrentLog(entry) { state.currentLog = { time: new Date(), ...entry }; notify() }

// Writes in the SAME format as the source file — CSV stays CSV, XLSX
// gets a real xlsx binary (was always CSV text before, even for .xlsx
// files — that was the bug). Uses a Blob (not a raw typed array), which
// FileSystemWritableFileStream.write() handles reliably across
// browsers. createWritable() writes to a swap file; close() is the
// atomic commit — if the write above throws, we never call close() and
// the real file on disk is untouched.
async function writeBackIfPossible() {
  if (!state.canLiveWrite || !state.fileHandle) return
  try {
    const ws = XLSX.utils.json_to_sheet(rowsWithStatusColumn())
    const writable = await state.fileHandle.createWritable()
    if (state.fileType === 'xlsx') {
      const wb = XLSX.utils.book_new()
      XLSX.utils.book_append_sheet(wb, ws, 'Calls')
      const arrayBuffer = XLSX.write(wb, { type: 'array', bookType: 'xlsx' })
      await writable.write(new Blob([arrayBuffer], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      }))
    } else {
      await writable.write(XLSX.utils.sheet_to_csv(ws))
    }
    await writable.close()
  } catch (err) { console.error('Live sheet write failed:', err) }
}

async function ensureCampaign(callHandlerUrl) {
  if (state.campaignId) return state.campaignId
  const leads = state.rows.map((r, i) => ({
    row_index: i, phone: r.__phone, name: state.nameKey ? r[state.nameKey] : undefined,
    raw_row: Object.fromEntries(state.columns.map(c => [c, r[c]])),
  }))
  const res = await fetch(`${callHandlerUrl}/api/campaigns`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent_id: state.agentId, file_name: state.fileName, leads }),
  })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail || 'Failed to create campaign')
  state.campaignId = data.campaign_id
  data.leads.forEach(l => { state.rows[l.row_index] = { ...state.rows[l.row_index], __lead_id: l.lead_id } })
  state.hasStarted = true
  notify()
  return state.campaignId
}

function startReconcileLoop(callHandlerUrl) {
  if (control.reconcileTimer) return
  control.reconcileTimer = setInterval(async () => {
    if (!state.campaignId) return
    try {
      await fetch(`${callHandlerUrl}/api/campaigns/${state.campaignId}/reconcile`, { method: 'POST' })
      // BUG FIX: reconcile used to only patch the backend row; the local
      // `state.rows` (what the table actually renders) was never synced,
      // so any row whose waitForOutcome() hit the 30s timeout stayed
      // stuck showing a stale status in the UI forever. Pull the
      // authoritative lead list back and patch local rows that drifted.
      const res = await fetch(`${callHandlerUrl}/api/campaigns/${state.campaignId}/leads`)
      const data = await res.json()
      if (!res.ok || !data.leads) return
      let changed = false
      data.leads.forEach(l => {
        const idx = state.rows.findIndex(r => r.__lead_id === l.lead_id)
        if (idx === -1) return
        const freshStatus = l.hangup_cause || (l.business_status === 'PENDING' ? BATCH_STATUSES.PENDING : l.business_status)
        const row = state.rows[idx]
        if (freshStatus && (freshStatus !== row.__status || (l.call_uuid && l.call_uuid !== row.__call_uuid))) {
          state.rows[idx] = { ...row, __status: freshStatus, __call_uuid: l.call_uuid || row.__call_uuid }
          changed = true
        }
      })
      if (changed) notify()
    } catch { /* non-fatal */ }
  }, 20000)
}
function stopReconcileLoop() {
  if (control.reconcileTimer) { clearInterval(control.reconcileTimer); control.reconcileTimer = null }
}

async function dialRow(row, rowIdx, voiceServerUrl, callHandlerUrl) {
  const to = toE164(row.__phone, state.countryCode)
  if (!to || to === '+' + state.countryCode || !isValidE164(to)) {
    updateRow(rowIdx, { __status: BATCH_STATUSES.FAILED })
    setCurrentLog({ row: rowIdx + 1, phone: row.__phone, message: 'Invalid/missing phone number', level: 'err' })
    return
  }

  let attemptId
  try {
    const r = await fetch(`${callHandlerUrl}/api/campaigns/${state.campaignId}/dial/${row.__lead_id}`, { method: 'POST' })
    const d = await r.json()
    if (!r.ok) throw new Error(d.detail || 'dial reservation failed')
    attemptId = d.attempt_id
    if (d.reused && d.call_uuid) {
      updateRow(rowIdx, { __call_uuid: d.call_uuid })
      setCurrentLog({ row: rowIdx + 1, phone: to, message: 'Resuming in-flight call (idempotent reuse)', level: 'info' })
      await waitForOutcome(d.call_uuid, rowIdx)
      return
    }
  } catch (e) {
    updateRow(rowIdx, { __status: BATCH_STATUSES.FAILED })
    setCurrentLog({ row: rowIdx + 1, phone: to, message: 'Failed: ' + e.message, level: 'err' })
    return
  }

  // BUGFIX (name propagation) — the backend (server.py's /api/outbound-call
  // -> place_outbound_call) has always accepted and forwarded a "name"
  // field through to the voice agent's greeting; this call just never
  // sent it. state.nameKey is the detected Name column from the sheet
  // (see parseWorkbookBinary); row still carries the original columns
  // via the ...row spread in loadFromFileInput/resumeCampaign. Falls
  // back to "" when there's no Name column, so calls without one still
  // dial exactly as before.
  const leadName = (state.nameKey ? row[state.nameKey] : row.name) || ''
  setCurrentLog({ row: rowIdx + 1, phone: to, message: 'Calling…', level: 'info' })
  try {
    const res = await fetch(`${voiceServerUrl}/api/outbound-call`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ to, agent_id: state.agentId, name: String(leadName).trim() }),
    })
    const data = await res.json()
    if (!res.ok || data.error) {
      updateRow(rowIdx, { __status: BATCH_STATUSES.FAILED })
      await fetch(`${callHandlerUrl}/api/campaigns/attempts/${attemptId}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ business_status: 'FAILED', failure_reason: data.error || res.statusText }),
      })
      setCurrentLog({ row: rowIdx + 1, phone: to, message: 'Failed: ' + (data.error || res.statusText), level: 'err' })
      return
    }
    let resolvedCallUuid = data.call_uuid
    if (!resolvedCallUuid && data.dash_id) {
      setCurrentLog({ row: rowIdx + 1, phone: to, message: 'Still connecting — waiting on answer webhook…', level: 'info' })
      resolvedCallUuid = await pollResolveCallUuid(voiceServerUrl, data.dash_id)
    }
    if (!resolvedCallUuid) {
      updateRow(rowIdx, { __status: BATCH_STATUSES.FAILED })
      await fetch(`${callHandlerUrl}/api/campaigns/attempts/${attemptId}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ business_status: 'FAILED', failure_reason: 'no call_uuid — answer webhook never fired' }),
      })
      setCurrentLog({ row: rowIdx + 1, phone: to, message: 'No call_uuid — answer webhook never fired', level: 'err' })
      return
    }
    data.call_uuid = resolvedCallUuid

    // BUGFIX — reflect "DIALING" locally right away instead of leaving the
    // row on "Pending" until the next 20s reconcile tick happens to sync
    // it. Without this, nextPendingWave() could pick the same still-
    // in-flight row again for another wave before reconcile ever ran.
    updateRow(rowIdx, { __call_uuid: data.call_uuid, __status: 'DIALING' })
    await fetch(`${callHandlerUrl}/api/campaigns/attempts/${attemptId}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ call_uuid: data.call_uuid, business_status: 'DIALING' }),
    })
    await waitForOutcome(data.call_uuid, rowIdx)
  } catch (e) {
    updateRow(rowIdx, { __status: BATCH_STATUSES.FAILED })
    setCurrentLog({ row: rowIdx + 1, phone: to, message: 'Failed: ' + e.message, level: 'err' })
  }
}

// BUGFIX (premature FAILED on slow-connecting calls) — server.py's
// place_outbound_call() already blocks up to ~25s server-side waiting
// for Plivo's answer webhook to resolve the real call_uuid, then
// returns call_uuid=null + a dash_id for the dashboard to keep
// resolving via /api/resolve-call-uuid if it's STILL not resolved by
// then (see that function's docstring). This dashboard never called
// that endpoint, so any call slower than the server's own 25s budget
// was marked Failed here even though it went on to connect. Poll for
// a further window before giving up for real.
async function pollResolveCallUuid(voiceServerUrl, dashId, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${voiceServerUrl}/api/resolve-call-uuid?dash_id=${encodeURIComponent(dashId)}`)
      const d = await r.json()
      if (r.ok && d.call_uuid) return d.call_uuid
    } catch { /* keep polling */ }
    await new Promise(res => setTimeout(res, 2000))
  }
  return null
}

async function fetchAttemptHangupCause(callUuid) {
  // Direct DB read, bypassing Realtime — used both to close the
  // subscribe-vs-already-finished race right after subscribing, and as
  // a last check before giving up at the timeout.
  try {
    const { data } = await supabase.from('call_attempts').select('hangup_cause').eq('call_uuid', callUuid).maybeSingle()
    return data?.hangup_cause || null
  } catch {
    return null
  }
}

async function waitForOutcome(callUuid, rowIdx, timeoutMs = 30000) {
  await new Promise((resolve) => {
    let settled = false
    const finish = (cause) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      channel.unsubscribe()
      updateRow(rowIdx, { __status: cause })
      setCurrentLog({ row: rowIdx + 1, message: cause, level: cause === 'Normal Hangup' ? 'ok' : 'info' })
      writeBackIfPossible().finally(resolve)
    }

    // RACE FIX — a fast call (create -> hangup -> webhook -> DB write,
    // all inside a second or two) can finish and have its Realtime
    // event fire BEFORE this subscription is even established. Without
    // an immediate DB check right after subscribing, that outcome is
    // silently missed and this row sits idle for the full 30s timeout
    // even though the DB already has the answer. Subscribe first (so we
    // don't miss anything from here forward), then immediately check
    // current state (to catch anything that already happened).
    const channel = supabase
      .channel(`call-status-${callUuid}`)
      .on('postgres_changes', { event: 'UPDATE', schema: 'public', table: 'call_attempts', filter: `call_uuid=eq.${callUuid}` },
        (payload) => {
          const cause = payload.new?.hangup_cause
          if (cause) finish(cause)
        })
      .subscribe(async (status) => {
        if (status !== 'SUBSCRIBED' || settled) return
        const cause = await fetchAttemptHangupCause(callUuid)
        if (cause) finish(cause)
      })

    const timer = setTimeout(async () => {
      if (settled) return
      // Never assume a missed Realtime event means the call is still
      // active — query Supabase one final time before giving up.
      const cause = await fetchAttemptHangupCause(callUuid)
      if (cause) { finish(cause); return }
      if (settled) return
      settled = true; channel.unsubscribe()
      setCurrentLog({ row: rowIdx + 1, message: 'Still ringing/in-progress after 30s — moving on, reconciler will catch the outcome', level: 'info' })
      resolve()
    }, timeoutMs)
  })
}

// One row's dial, wrapped so its OWN arrow clears the instant its own
// call resolves — independent of the rest of its concurrency wave.
async function dialRowTracked(rowIdx, voiceServerUrl, callHandlerUrl) {
  try {
    await dialRow(state.rows[rowIdx], rowIdx, voiceServerUrl, callHandlerUrl)
  } finally {
    state.activeIndexes.delete(rowIdx)
    notify()
  }
}

function nextPendingWave(n) {
  const out = []
  for (let i = 0; i < state.rows.length && out.length < n; i++) {
    if (state.rows[i].__status === BATCH_STATUSES.PENDING) out.push(i)
  }
  return out
}

export async function startBatch(voiceServerUrl, callHandlerUrl) {
  if (!state.rows.length || !state.agentId) return
  if (control.loopAlive) { control.pause = false; control.stopRequested = false; state.running = true; notify(); return }
  // SYNCHRONOUS lock — set before the first `await` below (ensureCampaign
  // is async). A second Start click that lands while ensureCampaign() is
  // still in flight sees state.starting===true here and bails, instead
  // of racing into its own ensureCampaign()/loop. Do not move this check
  // after any await — that's exactly the bug this fixes.
  if (state.starting) return
  state.starting = true; notify()

  try { await ensureCampaign(callHandlerUrl) } catch (e) {
    setCurrentLog({ row: 0, message: 'Campaign create failed: ' + e.message, level: 'err' })
    state.starting = false; notify()
    return
  }
  startReconcileLoop(callHandlerUrl)

  control.loopAlive = true; control.stopRequested = false; control.pause = false
  state.running = true; state.starting = false; notify()

  while (true) {
    while (control.pause && !control.stopRequested) { await new Promise(res => setTimeout(res, 500)) }
    if (control.stopRequested) break

    const wave = nextPendingWave(state.concurrency)
    if (!wave.length) break // no PENDING rows left — campaign done

    // WAVE SEMANTICS: launch `concurrency` calls together, wait for the
    // WHOLE wave before picking the next one — not a sliding pool that
    // starts a replacement the instant one call frees up.
    state.activeIndexes = new Set(wave); notify()
    await Promise.all(wave.map(i => dialRowTracked(i, voiceServerUrl, callHandlerUrl)))

    if (control.stopRequested) break
    await new Promise(res => setTimeout(res, 1000))
  }

  control.loopAlive = false; state.running = false; state.activeIndexes = new Set()
  // BUGFIX (Agent page batch table stuck on "Dialing") — this only checked
  // for the literal string "Pending". A row whose waitForOutcome() hit its
  // 30s timeout without a hangup_cause yet is NOT re-added to the dial
  // loop's pending list (nextPendingWave), so once every row has moved
  // off "Pending" — even rows still genuinely mid-call, showing "DIALING"/
  // "RINGING"/etc — the outer while(true) loop above exits and this used
  // to shut the reconcile interval down immediately. With no more
  // reconciliation, and that row's own Realtime subscription already
  // unsubscribed at the timeout, nothing was left to ever fetch its real
  // hangup cause — the row was frozen on "Dialing" for good. Now the
  // reconciler keeps polling until every row is in an actually-terminal
  // state, matching the backend's own NON_TERMINAL definition.
  const stillActive = state.rows.some(r => NON_TERMINAL_STATUSES.has(r.__status))
  if (!stillActive) stopReconcileLoop()
  notify()
}

export function pauseBatch() { control.pause = true; state.running = false; notify() }
export function stopBatch() { control.stopRequested = true; control.pause = false; stopReconcileLoop(); notify() }

export function exportBatchSheet() {
  if (!state.rows.length) return
  const ws = XLSX.utils.json_to_sheet(rowsWithStatusColumn())
  const wb = XLSX.utils.book_new()
  XLSX.utils.book_append_sheet(wb, ws, 'Calls')
  const base = state.fileName ? state.fileName.replace(/\.[^.]+$/, '') : 'batch_calls'
  XLSX.writeFile(wb, `${base}_status.xlsx`)
}

export async function listCampaigns(agentId, callHandlerUrl) {
  const res = await fetch(`${callHandlerUrl}/api/campaigns?agent_id=${encodeURIComponent(agentId)}`)
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail || 'Failed to list campaigns')
  return data.campaigns || []
}

export async function deleteCampaign(campaignId, callHandlerUrl) {
  const res = await fetch(`${callHandlerUrl}/api/campaigns/${campaignId}`, { method: 'DELETE' })
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail || 'Failed to delete campaign')
  return data
}

export async function resumeCampaign(campaignId, callHandlerUrl) {
  const res = await fetch(`${callHandlerUrl}/api/campaigns/${campaignId}/leads`)
  const data = await res.json()
  if (!res.ok) throw new Error(data.detail || 'Failed to load campaign')
  state.campaignId = campaignId; state.hasStarted = true
  state.columns = Object.keys(data.leads[0]?.raw_row || {})
  state.phoneKey = state.columns.find(c => PHONE_HEADER_CANDIDATES.includes(c.trim().toLowerCase())) || state.columns[0]
  state.rows = data.leads.map(l => ({
    __row: l.row_index + 1, __phone: l.phone, __lead_id: l.lead_id,
    __call_uuid: l.call_uuid || '',
    __status: l.hangup_cause || (l.business_status === 'PENDING' ? BATCH_STATUSES.PENDING : l.business_status),
    ...l.raw_row,
  }))
  state.activeIndexes = new Set()
  notify()
}