// src/components/PageAgentProfiles.jsx
import { useEffect, useState, useRef } from 'react'
import { PhoneCall, Plus, Trash2, Save, Play, Pause, Square, ArrowRight, Download, RefreshCw, Sparkles } from 'lucide-react'
import { supabase } from '../supabaseClient'
import styles from './Dashboard.module.css'
import { CALL_HANDLER_URL } from './dashboardShared'

// Batch calling state/loop lives in ./batchCallStore (module-level,
// survives page navigation). This component only renders `batch`
// (a read-only snapshot) and calls the store's exported functions.
import {
  useBatchStore, COUNTRY_CODES, toE164, CONCURRENCY_OPTIONS,
  setCountryCode as setBatchCountryCode, setAgentId as setBatchAgentId,
  setConcurrency as setBatchConcurrency,
  loadFromFileInput, loadFromFilePicker, loadFromGoogleSheetCsvUrl,
  startBatch, pauseBatch, endBatch, exportBatchSheet, validateRows, applyPreflightSkips,
  listCampaigns, resumeCampaign, deleteCampaign,
} from './batchCallStore'

// base URL for the voice server (server.py) — separate from
// CALL_HANDLER_URL (call_handler.py).
const VOICE_SERVER_URL = import.meta.env.VITE_VOICE_SERVER_URL || 'http://localhost:8080'

// Starter prompts for non-technical users creating a new agent. Each is a
// solid, editable starting point — not meant to be used verbatim. Picking
// one just fills the textarea; nothing is saved until "Save Prompt".
const PROMPT_TEMPLATES = [
  {
    id: 'outbound_sales',
    label: 'Outbound Sales Caller',
    description: 'Cold-calls a lead, introduces the product, and books a follow-up.',
    text: `You are a friendly, professional outbound sales representative calling on behalf of [Company Name].

Goal: introduce [Product/Service], gauge interest, and book a follow-up call or demo if the lead is interested.

Guidelines:
- Open with a brief, warm introduction and state the reason for the call in one sentence.
- Ask 1-2 open questions to understand the lead's needs before pitching.
- Keep responses short and conversational — this is a phone call, not an email.
- If the lead is not interested, thank them politely and end the call gracefully.
- If the lead is interested, confirm a specific day/time for a follow-up.
- Never make promises about pricing or contracts you're not authorized to make.
- If asked something you don't know, say you'll have a team member follow up rather than guessing.`,
  },
  {
    id: 'appointment_setter',
    label: 'Appointment Setter',
    description: 'Focused purely on booking a meeting or demo slot.',
    text: `You are an appointment-setting assistant calling on behalf of [Company Name].

Goal: secure a confirmed appointment on the lead's calendar for a [demo/consultation] — nothing more.

Guidelines:
- Keep the call short. Don't pitch the full product — just enough to earn the appointment.
- Offer 2-3 specific time windows rather than asking "when works for you" open-endedly.
- Confirm the lead's name, callback number, and preferred time before ending the call.
- If the lead hesitates, ask one clarifying question about their availability, then offer alternate times.
- If they decline, thank them and end politely — do not push further on this call.`,
  },
  {
    id: 'lead_qualifier',
    label: 'Lead Qualifier',
    description: 'Asks a short set of questions to score how good a fit the lead is.',
    text: `You are a lead-qualification assistant calling on behalf of [Company Name].

Goal: determine, through a short set of questions, whether this lead is a good fit for [Product/Service], and pass qualified leads on to the sales team.

Ask about (naturally, one at a time, not as a rigid checklist):
- What problem they're currently trying to solve
- Their rough budget range or timeline
- Who else is involved in this decision

Guidelines:
- Sound curious and helpful, not like an interrogation.
- Keep the call under a few minutes.
- If they clearly aren't a fit, thank them for their time and end the call politely.
- If they are a fit, let them know a specialist will follow up, and confirm the best contact method.`,
  },
  {
    id: 'support_followup',
    label: 'Customer Support Follow-up',
    description: 'Checks in after a purchase or support ticket to confirm satisfaction.',
    text: `You are a customer care assistant calling on behalf of [Company Name] to follow up after a recent [purchase/support ticket].

Goal: confirm the customer's issue was resolved and they're satisfied, and flag anything that needs further attention.

Guidelines:
- Start by referencing the specific order/ticket so the customer knows this isn't a cold call.
- Ask directly whether their issue was resolved.
- If yes, thank them and ask if there's anything else you can help with.
- If no, apologize, note the details, and let them know a team member will personally follow up.
- Stay calm and empathetic if the customer is frustrated — never get defensive.`,
  },
  {
    id: 'payment_reminder',
    label: 'Payment / Renewal Reminder',
    description: 'Reminds a customer about an upcoming or overdue payment or renewal.',
    text: `You are a billing assistant calling on behalf of [Company Name] regarding an upcoming [payment/renewal].

Goal: remind the customer of the amount due and date, and help them complete or schedule payment.

Guidelines:
- Be polite and non-confrontational — this is a reminder, not a collections call.
- State the amount and due date clearly and early in the call.
- Offer to help them pay now, or ask if they'd like to arrange a different date.
- If they say they've already paid, thank them and note it for the team to verify — do not argue.
- Never ask for full card numbers or sensitive payment details over this call; direct them to a secure payment link instead.`,
  },
]

export default function PageAgentProfiles({ showToast }) {
  const [agents, setAgents] = useState([])
  const [agentsLoading, setAgentsLoading] = useState(true)
  const [selectedId, setSelectedId] = useState('')

  const [name, setName] = useState('')
  const [phoneNumber, setPhoneNumber] = useState('')
  const [prompt, setPrompt] = useState('')
  const [promptLoading, setPromptLoading] = useState(false)
  const [savingPrompt, setSavingPrompt] = useState(false)
  const [dirty, setDirty] = useState(false)

  const [showCreate, setShowCreate] = useState(false)
  const [newId, setNewId] = useState('')
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)

  const [rollback, setRollback] = useState([])
  const [hoveredLogId, setHoveredLogId] = useState(null)

  const [plivoNumbers, setPlivoNumbers] = useState([])
  const [plivoLoading, setPlivoLoading] = useState(false)
  const [plivoError, setPlivoError] = useState('')
  const [linkingNumber, setLinkingNumber] = useState(null)

  async function loadPlivoNumbers() {
    setPlivoLoading(true)
    setPlivoError('')
    try {
      const res = await fetch(`${CALL_HANDLER_URL}/api/plivo/numbers`)
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Failed to fetch numbers')
      setPlivoNumbers(data.numbers || [])
    } catch (e) {
      setPlivoError(e.message)
    }
    setPlivoLoading(false)
  }

  useEffect(() => { loadPlivoNumbers() }, [])

  async function linkNumberToSelected(number, region) {
    if (!selectedId) return
    setLinkingNumber(number)
    try {
      const res = await fetch(`${CALL_HANDLER_URL}/api/plivo/link-number`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_id: selectedId, number, region }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Link failed')
      showToast(`${number} → ${name || selectedId} ✓`)
      await loadPlivoNumbers()
    } catch (e) {
      showToast('Link failed: ' + e.message, 'err')
    }
    setLinkingNumber(null)
  }

  async function unlinkNumber(number) {
    setLinkingNumber(number)
    try {
      const res = await fetch(`${CALL_HANDLER_URL}/api/plivo/unlink-number`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ number }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Unlink failed')
      showToast(`${number} unlinked`)
      await loadPlivoNumbers()
    } catch (e) {
      showToast('Unlink failed: ' + e.message, 'err')
    }
    setLinkingNumber(null)
  }

  const [togglingActive, setTogglingActive] = useState(false)
  const [activeCount, setActiveCount] = useState(null)

  async function loadActiveCount() {
    try {
      const res = await fetch(`${CALL_HANDLER_URL}/api/agents/active-count`)
      const data = await res.json()
      if (res.ok) setActiveCount(data)
    } catch (e) {
      // non-fatal
    }
  }

  useEffect(() => { loadActiveCount() }, [])

  async function toggleAgentActive(nextValue) {
    if (!selectedId) return
    setTogglingActive(true)
    try {
      const res = await fetch(`${CALL_HANDLER_URL}/api/agents/${selectedId}/toggle`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: nextValue }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Toggle failed')

      setAgents(prev => prev.map(a => a.agent_id === selectedId ? { ...a, is_active: nextValue } : a))
      showToast(`${name || selectedId} ${nextValue ? 'activated' : 'deactivated'} ✓`)
      await loadActiveCount()
    } catch (e) {
      showToast('Toggle failed: ' + e.message, 'err')
    }
    setTogglingActive(false)
  }

  async function loadAgents(selectAfter) {
    setAgentsLoading(true)
    const { data, error } = await supabase
      .from('agents')
      .select('agent_id, name, phone_number, is_active')
      .order('created_at', { ascending: true })

    if (error) {
      showToast('Failed to load agents: ' + error.message, 'err')
      setAgentsLoading(false)
      return
    }

    const list = data || []
    setAgents(list)
    setAgentsLoading(false)

    if (selectAfter) {
      setSelectedId(selectAfter)
    } else if (!selectedId && list.length) {
      setSelectedId(list[0].agent_id)
    }
  }

  useEffect(() => { loadAgents() }, [])

  useEffect(() => {
    if (!selectedId) return

    const a = agents.find(x => x.agent_id === selectedId)
    setName(a?.name || '')
    setPhoneNumber(a?.phone_number || '')

    setPromptLoading(true)
    setDirty(false)

    supabase
      .from('agent_config')
      .select('value')
      .eq('agent_id', selectedId)
      .eq('key', 'system_prompt')
      .maybeSingle()
      .then(({ data, error }) => {
        if (error) {
          showToast('Failed to load prompt: ' + error.message, 'err')
        }
        setPrompt(data?.value || '')
        setPromptLoading(false)
      })

    supabase
      .from('prompt_versions')
      .select('id, prompt_value, rollback_note, created_at')
      .eq('agent_id', selectedId)
      .order('created_at', { ascending: false })
      .limit(3)
      .then(({ data, error }) => {
        if (!error) setRollback(data || [])
      })
  }, [selectedId, agents])

  async function saveField(field, value) {
    const { error } = await supabase
      .from('agents')
      .update({ [field]: value || null })
      .eq('agent_id', selectedId)

    if (error) {
      showToast('Save failed: ' + error.message, 'err')
      return
    }
    setAgents(prev => prev.map(a => a.agent_id === selectedId ? { ...a, [field]: value || null } : a))
  }

  async function savePrompt() {
    setSavingPrompt(true)

    const { error } = await supabase
      .from('agent_config')
      .upsert(
        { agent_id: selectedId, key: 'system_prompt', value: prompt, updated_at: new Date().toISOString() },
        { onConflict: 'agent_id,key' }
      )

    if (error) {
      setSavingPrompt(false)
      showToast('Prompt save failed: ' + error.message, 'err')
      return
    }

    await supabase.from('prompt_versions').insert({
      agent_id: selectedId,
      prompt_key: 'system_prompt',
      prompt_value: prompt,
      rollback_note: 'Manual update',
    })

    // Keep only the 3 most recent history rows per agent — as a new one
    // arrives, the oldest one past 3 gets deleted. 3 old versions + the
    // 1 currently-active system_prompt = 4 states available total.
    const { data: allVersions } = await supabase
      .from('prompt_versions')
      .select('id')
      .eq('agent_id', selectedId)
      .order('created_at', { ascending: false })

    if (allVersions && allVersions.length > 3) {
      const idsToPrune = allVersions.slice(3).map(v => v.id)
      await supabase.from('prompt_versions').delete().in('id', idsToPrune)
    }

    setSavingPrompt(false)
    setDirty(false)
    showToast('Prompt saved ✓')

    const { data } = await supabase
      .from('prompt_versions')
      .select('id, prompt_value, rollback_note, created_at')
      .eq('agent_id', selectedId)
      .order('created_at', { ascending: false })
      .limit(3)
    setRollback(data || [])
  }

  async function rollbackTo(value) {
    setPrompt(value)
    setDirty(true)
  }

  async function deleteHistoryEntry(logId) {
    if (!window.confirm('Delete this saved prompt version? This cannot be undone.')) return
    const { error } = await supabase.from('prompt_versions').delete().eq('id', logId)
    if (error) {
      showToast('Delete failed: ' + error.message, 'err')
      return
    }
    setRollback(prev => prev.filter(r => r.id !== logId))
    showToast('Version deleted')
  }

  async function createAgent() {
    const id = newId.trim().toLowerCase().replace(/[^a-z0-9_]/g, '_')
    if (!id || !newName.trim()) {
      showToast('Agent ID and name are required', 'err')
      return
    }

    setCreating(true)

    const { error: agentErr } = await supabase
      .from('agents')
      .insert({ agent_id: id, name: newName.trim() })

    if (agentErr) {
      setCreating(false)
      showToast('Create failed: ' + agentErr.message, 'err')
      return
    }

    const { error: cfgErr } = await supabase
      .from('agent_config')
      .insert({ agent_id: id, key: 'system_prompt', value: '', updated_at: new Date().toISOString() })

    if (cfgErr) {
      setCreating(false)
      showToast('Agent created, but prompt row failed: ' + cfgErr.message, 'err')
    } else {
      showToast(`Agent "${newName.trim()}" created ✓ — assign a number below`)
    }

    setCreating(false)
    setShowCreate(false)
    setNewId(''); setNewName('')
    await loadAgents(id)
    await loadActiveCount()
  }

  async function deleteAgent() {
    if (selectedId === 'default') { showToast('Cannot delete the default agent', 'err'); return }
    if (!window.confirm(`Delete agent "${name || selectedId}"? This removes its prompt and any numbers assigned to it.`)) return

    await supabase.from('agent_config').delete().eq('agent_id', selectedId)
    await supabase.from('prompt_versions').delete().eq('agent_id', selectedId)
    await supabase.from('agent_numbers').delete().eq('agent_id', selectedId)
    const { error } = await supabase.from('agents').delete().eq('agent_id', selectedId)

    if (error) {
      showToast('Delete failed: ' + error.message, 'err')
      return
    }

    showToast('Agent deleted')
    await loadAgents('default')
    await loadActiveCount()
    await loadPlivoNumbers()
  }

  // ────────────────────────────────────────────────────────────
  // Single-call dialer — hits server.py's POST /api/outbound-call
  // directly (to, agent_id, name).
  // ────────────────────────────────────────────────────────────
  const [dialCountryCode, setDialCountryCode] = useState('91')
  const [dialTo, setDialTo] = useState('')
  const [dialName, setDialName] = useState('') // customer name → LLM lead_name for this call
  const [dialAgentId, setDialAgentId] = useState('')
  const [dialing, setDialing] = useState(false)
  const [callStatus, setCallStatus] = useState(null) // { status, message, time }

  useEffect(() => {
    if (!dialAgentId && agents.length) setDialAgentId(agents[0].agent_id)
  }, [agents, dialAgentId])

  // Pick up a "Follow-up Call" handoff from the Leads page — it stashes
  // the lead's number (+ name) in sessionStorage before sending the admin
  // here, so the single-call box arrives pre-filled and Call is one click.
  useEffect(() => {
    const pendingNumber = sessionStorage.getItem('pendingCallNumber')
    if (!pendingNumber) return
    const pendingName = sessionStorage.getItem('pendingCallName') || ''
    setDialTo(pendingNumber)
    setDialName(pendingName)
    sessionStorage.removeItem('pendingCallNumber')
    sessionStorage.removeItem('pendingCallName')
    showToast('Number loaded from lead — hit Call')
  }, [])

  async function placeCall() {
    // dialTo may already be a full E.164 number (e.g. handed off from the
    // Leads page's Follow-up Call action) — don't re-mangle it with the
    // country-code dropdown in that case.
    const to = dialTo.trim().startsWith('+') ? dialTo.trim() : toE164(dialTo, dialCountryCode)
    if (!dialTo.trim()) { showToast('Enter a phone number to call', 'err'); return }
    if (!dialAgentId) { showToast('Select an agent', 'err'); return }

    const agentLabel = agents.find(a => a.agent_id === dialAgentId)?.name || dialAgentId

    setDialing(true)
    setCallStatus({ status: 'dialing', message: `Dialing ${to} via ${agentLabel}${dialName.trim() ? ` (${dialName.trim()})` : ''}…`, time: new Date() })

    try {
      const res = await fetch(`${VOICE_SERVER_URL}/api/outbound-call`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to, agent_id: dialAgentId, name: dialName.trim() }),
      })
      const data = await res.json()

      if (!res.ok || data.error) {
        setCallStatus({ status: 'failed', message: data.error || 'Call failed', time: new Date() })
        showToast('Call failed: ' + (data.error || res.statusText), 'err')
      } else if (!data.call_uuid) {
        setCallStatus({
          status: 'failed',
          message: `No answer webhook received from Plivo — call likely never connected (invalid number / carrier reject). from=${data.from || '—'}`,
          time: new Date(),
        })
        showToast('Call did not connect', 'err')
      } else {
        setCallStatus({
          status: 'placed',
          message: `Call placed ✓ from=${data.from || '—'} call_uuid=${data.call_uuid}`,
          time: new Date(),
        })
        showToast(`Call to ${to} placed ✓`)
        setTimeout(() => setCallStatus(null), 30000)
      }
    } catch (e) {
      setCallStatus({ status: 'failed', message: e.message, time: new Date() })
      showToast('Call failed: ' + e.message, 'err')
    }

    setDialing(false)
  }

  const statusColor = {
    dialing: 'var(--accent)',
    placed: '#4ade80',
    failed: 'var(--hot)',
  }

  // ────────────────────────────────────────────────────────────
  // BATCH CALLING — all state + the dialing loop live in
  // batchCallStore.js. This component only renders `batch`.
  // ────────────────────────────────────────────────────────────
  const batch = useBatchStore()
  const batchTableBodyRef = useRef(null)
  const activeRowRef = useRef(null)

  useEffect(() => {
    const first = [...batch.activeIndexes][0]
    if (first !== undefined && activeRowRef.current) {
      activeRowRef.current.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
    }
  }, [batch.activeIndexes])

  useEffect(() => {
    if (!batch.agentId && agents.length) setBatchAgentId(agents[0].agent_id)
  }, [agents, batch.agentId])

  async function handleBatchFile(e) {
    const file = e.target.files?.[0]
    if (!file) return
    try {
      await loadFromFileInput(file)
      showToast(`Loaded ✓ — no live disk write from this picker (use "Load with live write" for that)`)
    } catch (err) {
      showToast('Failed to parse file: ' + err.message, 'err')
    }
    e.target.value = '' // allow re-selecting the same file
  }

  // Chrome/Edge only — opens the SAME file for read+write, so every
  // status update writes straight back into the source file on disk.
  async function handleBatchFileLiveWrite() {
    try {
      await loadFromFilePicker()
      showToast('Loaded ✓ — live-writing status back to the source file')
    } catch (err) {
      showToast(err.message, 'err')
    }
  }

  const [sheetsUrl, setSheetsUrl] = useState('')
  const [sheetsLoading, setSheetsLoading] = useState(false)

  async function importFromGoogleSheets() {
    if (!sheetsUrl.trim()) { showToast('Paste a Google Sheets link first', 'err'); return }
    if (batch.running) { showToast('Stop the current batch first', 'err'); return }
    setSheetsLoading(true)
    try {
      await loadFromGoogleSheetCsvUrl(sheetsUrl.trim())
      showToast('Sheet imported ✓')
    } catch (e) {
      showToast('Google Sheets import failed: ' + e.message, 'err')
    }
    setSheetsLoading(false)
  }

  // Resume a past campaign after a full page reload — the campaign lives
  // in the DB (campaigns/campaign_leads/call_attempts), so it's still
  // there even though batchCallStore's in-memory `rows` was wiped by the
  // reload. This is the only way to get back to it.
  const [recentCampaigns, setRecentCampaigns] = useState([])
  const [campaignsLoading, setCampaignsLoading] = useState(false)
  const [pickedCampaignId, setPickedCampaignId] = useState('')

  async function loadRecentCampaigns() {
    if (!batch.agentId) return
    setCampaignsLoading(true)
    try {
      const list = await listCampaigns(batch.agentId, CALL_HANDLER_URL)
      setRecentCampaigns(list)
    } catch (e) {
      showToast('Failed to load past campaigns: ' + e.message, 'err')
    }
    setCampaignsLoading(false)
  }

  useEffect(() => { loadRecentCampaigns() }, [batch.agentId])

  async function handleResumePastCampaign() {
    if (!pickedCampaignId) return
    try {
      await resumeCampaign(pickedCampaignId, CALL_HANDLER_URL)
      showToast('Campaign loaded ✓ — hit Resume to continue dialing')
    } catch (e) {
      showToast('Resume failed: ' + e.message, 'err')
    }
  }

  async function handleDeleteCampaign(campaignId, fileName) {
    if (campaignId === batch.campaignId && batch.running) {
      showToast("Can't delete the campaign that's currently running", 'err')
      return
    }
    if (!window.confirm(`Delete batch "${fileName || campaignId.slice(0, 8)}"? Dialing history for it is gone for good — HOT leads already saved to your dashboard are untouched.`)) return
    try {
      await deleteCampaign(campaignId, CALL_HANDLER_URL)
      if (campaignId === pickedCampaignId) setPickedCampaignId('')
      showToast('Batch deleted ✓')
      await loadRecentCampaigns()
    } catch (e) {
      showToast('Delete failed: ' + e.message, 'err')
    }
  }

  function handleStartOrResume() {
    if (!batch.rows.length) { showToast('Upload a sheet first', 'err'); return }
    if (!batch.agentId) { showToast('Select an agent', 'err'); return }

    // pre-flight — check once per fresh load; skip re-checking on Resume
    // (rows already validated, re-running would just recompute the same).
    if (!batch.hasStarted) {
      const pf = validateRows()
      if (pf.invalidRows.length || pf.duplicateRows.length) {
        const ok = window.confirm(
          `Pre-flight: ${pf.validCount}/${pf.total} valid rows. ` +
          `${pf.invalidRows.length} invalid number(s), ${pf.duplicateRows.length} duplicate(s) — ` +
          `these rows will be marked Failed/skipped. Start anyway?`
        )
        if (!ok) return
        applyPreflightSkips()
      }
    }

    const resuming = batch.hasStarted
    startBatch(VOICE_SERVER_URL, CALL_HANDLER_URL)
    showToast(resuming ? 'Batch resumed' : `Batch calling started — ${batch.concurrency} at a time`)
  }

  function handlePause() {
    // Semantics: no new calls launch after this; any call(s) already
    // in flight (the current wave) are left to finish naturally — see
    // pauseBatch()/startBatch()'s wave loop in batchCallStore.js. This
    // never hangs up a live sales conversation.
    pauseBatch()
    showToast(batch.activeIndexes.size ? 'Pausing — current call(s) will finish' : 'Batch paused')
  }

  function handleEnd() {
    // FIX: Pause and the old Stop were functionally near-identical — both
    // just halted the loop and left Resume/Start able to pick the exact
    // same rows back up. This is the real distinction the two buttons
    // needed: Pause is resumable, End is not. Any call(s) already in
    // flight are left alone either way — this never hangs up a live
    // conversation — but the uploaded sheet and campaign link are wiped,
    // so a fresh upload is required to run anything again.
    const ok = window.confirm('End this batch? The uploaded sheet and its progress will be cleared — this cannot be undone.')
    if (!ok) return
    endBatch()
    showToast(batch.activeIndexes.size ? 'Batch ended — current call(s) will finish, sheet cleared' : 'Batch ended and cleared')
  }

  function handleExport() {
    if (!batch.rows.length) { showToast('Nothing to export', 'err'); return }
    exportBatchSheet()
    showToast('Sheet exported ✓')
  }

  // Colors for the real Plivo-CDR-derived statuses (batchCallStore.js
  // Status is now the raw Plivo hangup_cause (or a local Pending/Failed/
  // Unknown placeholder) — color by a few recognizable cases, default
  // to plain text for whatever else Plivo's log reports.
  function batchStatusColor(status) {
    if (status === 'Pending') return 'var(--text3)'
    if (status === 'Failed' || status === 'Unknown') return 'var(--hot)'
    if (status === 'Skipped (duplicate)') return 'var(--warm)'
    if (status === 'Normal Hangup') return '#4ade80'
    return 'var(--text1)'
  }

  const inputStyle = {
    width: '100%', background: 'var(--bg3)', border: '0.5px solid var(--border)',
    borderRadius: 7, padding: '8px 10px', color: 'var(--text1)', fontSize: 13, outline: 'none',
  }

  const selectedAgentObj = agents.find(a => a.agent_id === selectedId)
  const isActive = selectedAgentObj?.is_active ?? true

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>

      {activeCount && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--text2)' }}>
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: activeCount.count > 0 ? '#4ade80' : 'var(--hot)' }} />
          <strong style={{ color: 'var(--text1)' }}>{activeCount.count} of {activeCount.total}</strong> agents active in the call pool
        </div>
      )}

      {/* DIALER CARD — call directly from the dashboard via server.py */}
      <div style={{ background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 12, padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5 }}>
          Place Call
        </span>

        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div style={{ flex: '0 0 150px' }}>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
              Country
            </label>
            <select value={dialCountryCode} onChange={e => setDialCountryCode(e.target.value)} style={inputStyle}>
              {COUNTRY_CODES.map(c => <option key={c.code} value={c.code}>{c.label}</option>)}
            </select>
          </div>

          <div style={{ flex: '1 1 220px' }}>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
              Phone Number
            </label>
            <input
              value={dialTo}
              onChange={e => setDialTo(e.target.value)}
              placeholder="e.g. 9876543210"
              style={inputStyle}
            />
          </div>

          <div style={{ flex: '1 1 200px' }}>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
              Customer Name <span style={{ color: 'var(--text3)', textTransform: 'none', fontWeight: 400 }}>(optional)</span>
            </label>
            <input
              value={dialName}
              onChange={e => setDialName(e.target.value)}
              placeholder="e.g. Priya Sharma"
              style={inputStyle}
            />
          </div>

          <div style={{ flex: '1 1 220px' }}>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
              Agent
            </label>
            <select
              value={dialAgentId}
              onChange={e => setDialAgentId(e.target.value)}
              disabled={agentsLoading}
              style={inputStyle}
            >
              {agentsLoading && <option>Loading…</option>}
              {!agentsLoading && agents.length === 0 && <option>No agents found</option>}
              {agents.map(a => (
                <option key={a.agent_id} value={a.agent_id}>
                  {a.name || '—'} ({a.agent_id}){a.is_active === false ? ' — inactive' : ''}
                </option>
              ))}
            </select>
          </div>

          <button
            onClick={placeCall}
            disabled={dialing || !dialTo.trim() || !dialAgentId}
            style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '9px 18px',
              background: dialing ? 'var(--bg3)' : 'var(--accent)', border: 'none', borderRadius: 8,
              color: dialing ? 'var(--text3)' : '#fff', fontSize: 13, fontWeight: 600,
              cursor: dialing ? 'default' : 'pointer', opacity: (!dialTo.trim() || !dialAgentId) ? 0.6 : 1,
            }}
          >
            <PhoneCall size={14} /> {dialing ? 'Dialing…' : 'Call'}
          </button>
        </div>

        {dialTo.trim() && (
          <p style={{ fontSize: 11, color: 'var(--text3)', margin: 0 }}>
            Will dial as: <span style={{ fontFamily: 'monospace', color: 'var(--text2)' }}>{toE164(dialTo, dialCountryCode)}</span>
            {dialName.trim() && <> — greeting will use <span style={{ color: 'var(--text2)' }}>"{dialName.trim()}"</span></>}
          </p>
        )}

        {callStatus && (
          <div style={{ borderTop: '0.5px solid var(--border)', paddingTop: 10, display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
            <span style={{ width: 7, height: 7, borderRadius: '50%', background: statusColor[callStatus.status] || 'var(--text3)', flexShrink: 0 }} />
            <span style={{ color: 'var(--text3)' }}>{callStatus.time.toLocaleTimeString()}</span>
            <span style={{ color: 'var(--text1)' }}>{callStatus.message}</span>
          </div>
        )}
      </div>

      {/* BATCH CALLING CARD */}
      <div style={{ background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 12, padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5 }}>
            Batch Calling
          </span>
          {batch.canLiveWrite && (
            <span style={{ fontSize: 10, color: '#4ade80', display: 'flex', alignItems: 'center', gap: 4 }}>
              ● live-writing to disk
            </span>
          )}
        </div>

        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'flex-end' }}>
          <div style={{ flex: '0 0 150px' }}>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
              Country
            </label>
            <select value={batch.countryCode} onChange={e => setBatchCountryCode(e.target.value)} style={inputStyle} disabled={batch.running}>
              {COUNTRY_CODES.map(c => <option key={c.code} value={c.code}>{c.label}</option>)}
            </select>
          </div>

          <div style={{ flex: '1 1 220px' }}>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
              Agent
            </label>
            <select value={batch.agentId} onChange={e => setBatchAgentId(e.target.value)} style={inputStyle} disabled={batch.running || agentsLoading}>
              {agentsLoading && <option>Loading…</option>}
              {agents.map(a => (
                <option key={a.agent_id} value={a.agent_id}>{a.name || '—'} ({a.agent_id})</option>
              ))}
            </select>
          </div>

          <div style={{ flex: '0 0 140px' }}>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
              Concurrency
            </label>
            <select
              value={batch.concurrency}
              onChange={e => setBatchConcurrency(e.target.value)}
              style={inputStyle}
              disabled={batch.running}
              title="How many calls dial at once, per wave"
            >
              {CONCURRENCY_OPTIONS.map(n => <option key={n} value={n}>{n} at a time</option>)}
            </select>
          </div>

          <div style={{ flex: '1 1 220px' }}>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
              Sheet (.xlsx / .csv)
            </label>
            <input
              type="file"
              accept=".xlsx,.xls,.csv"
              onChange={handleBatchFile}
              disabled={batch.running}
              style={{ ...inputStyle, padding: '6px' }}
            />
          </div>

          <button
            onClick={handleBatchFileLiveWrite}
            disabled={batch.running}
            title="Chrome/Edge only — writes call status back into this same file as the batch runs"
            style={{ padding: '8px 12px', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--text2)', fontSize: 12, fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap', opacity: batch.running ? 0.5 : 1 }}
          >
            Load with live write
          </button>
        </div>

        {/* Google Sheets import — accepts a normal Share link or a
            published-to-web CSV link (see batchCallStore.js normalizeSheetsCsvUrl) */}
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end' }}>
          <div style={{ flex: '1 1 320px' }}>
            <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
              …or Google Sheets link (shared or published-to-web)
            </label>
            <input
              value={sheetsUrl}
              onChange={e => setSheetsUrl(e.target.value)}
              placeholder="https://docs.google.com/spreadsheets/d/…"
              disabled={batch.running || sheetsLoading}
              style={inputStyle}
            />
          </div>
          <button
            onClick={importFromGoogleSheets}
            disabled={batch.running || sheetsLoading || !sheetsUrl.trim()}
            style={{ padding: '8px 14px', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--text1)', fontSize: 12, fontWeight: 600, cursor: 'pointer', opacity: (!sheetsUrl.trim() || sheetsLoading) ? 0.6 : 1, whiteSpace: 'nowrap' }}
          >
            {sheetsLoading ? 'Loading…' : 'Import'}
          </button>
        </div>
        <p style={{ fontSize: 10, color: 'var(--text3)', margin: 0 }}>
          Read-only import — this pulls a snapshot of the sheet. Status updates write to the local table / disk / export, not back to the original Google Sheet.
          Add a "Name" column to have each row's lead name passed to the LLM automatically.
        </p>

        {/* RESUME A PAST CAMPAIGN — campaigns live in the DB, so this
            survives closing the tab entirely, unlike everything above.
            Delete frees space once a batch is done — HOT leads are
            already saved separately in `calls`, unaffected by this. */}
        <div style={{ borderTop: '0.5px solid var(--border)', paddingTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5 }}>
              Past campaigns for this agent
            </span>
            <button
              onClick={loadRecentCampaigns}
              disabled={campaignsLoading}
              title="Refresh"
              style={{ padding: '4px 6px', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 6, color: 'var(--text2)', cursor: 'pointer' }}
            >
              <RefreshCw size={12} className={campaignsLoading ? styles.spin : ''} />
            </button>
          </div>

          {campaignsLoading && recentCampaigns.length === 0 && (
            <p style={{ fontSize: 12, color: 'var(--text3)', margin: 0 }}>Loading…</p>
          )}
          {!campaignsLoading && recentCampaigns.length === 0 && (
            <p style={{ fontSize: 12, color: 'var(--text3)', margin: 0 }}>No past campaigns for this agent.</p>
          )}

          {recentCampaigns.length > 0 && (
            <div style={{ maxHeight: 160, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {recentCampaigns.map(c => (
                <div key={c.campaign_id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px', background: 'var(--bg3)', borderRadius: 8, fontSize: 12 }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <span style={{ color: 'var(--text1)' }}>{c.file_name || c.campaign_id.slice(0, 8)}</span>
                    <span style={{ color: 'var(--text3)', marginLeft: 8 }}>{c.total_leads} leads · {new Date(c.created_at).toLocaleString()}</span>
                  </div>
                  <button
                    onClick={() => { setPickedCampaignId(c.campaign_id); handleResumePastCampaign() }}
                    disabled={batch.running}
                    style={{ padding: '5px 10px', background: 'var(--accent)', border: 'none', borderRadius: 6, color: '#fff', fontSize: 11, fontWeight: 600, cursor: 'pointer', opacity: batch.running ? 0.5 : 1 }}
                  >
                    Load
                  </button>
                  <button
                    onClick={() => handleDeleteCampaign(c.campaign_id, c.file_name)}
                    disabled={batch.running && c.campaign_id === batch.campaignId}
                    title="Delete this batch"
                    style={{ padding: '5px 8px', background: 'transparent', border: '0.5px solid var(--border)', borderRadius: 6, color: 'var(--hot)', cursor: 'pointer', opacity: (batch.running && c.campaign_id === batch.campaignId) ? 0.4 : 1 }}
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {batch.rows.length > 0 && (
          <p style={{ fontSize: 11, color: 'var(--text3)', margin: 0 }}>
            {batch.fileName} — {batch.rows.length} rows — phone column: <span style={{ color: 'var(--text2)', fontFamily: 'monospace' }}>{batch.phoneKey}</span>
            {batch.nameKey && <> — name column: <span style={{ color: 'var(--text2)', fontFamily: 'monospace' }}>{batch.nameKey}</span></>}
          </p>
        )}

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          {!batch.running ? (
            <button
              onClick={handleStartOrResume}
              disabled={!batch.rows.length || !batch.agentId || batch.starting}
              style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 16px', background: 'var(--accent)', border: 'none', borderRadius: 8, color: '#fff', fontSize: 13, fontWeight: 600, cursor: 'pointer', opacity: (!batch.rows.length || !batch.agentId || batch.starting) ? 0.6 : 1 }}
            >
              <Play size={14} /> {batch.starting ? 'Starting…' : (batch.hasStarted ? 'Resume' : 'Start') + ' Calling'}
            </button>
          ) : (
            <button
              onClick={handlePause}
              style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 16px', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--text1)', fontSize: 13, fontWeight: 600, cursor: 'pointer' }}
            >
              <Pause size={14} /> Pause
            </button>
          )}
          <button
            onClick={handleEnd}
            disabled={!batch.running && !batch.hasStarted}
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 16px', background: 'transparent', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--hot)', fontSize: 13, fontWeight: 600, cursor: 'pointer', opacity: (!batch.running && !batch.hasStarted) ? 0.5 : 1 }}
          >
            <Square size={14} /> End
          </button>
          <button
            onClick={handleExport}
            disabled={!batch.rows.length}
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 16px', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--text1)', fontSize: 13, fontWeight: 600, cursor: 'pointer', opacity: !batch.rows.length ? 0.5 : 1, marginLeft: 'auto' }}
          >
            <Download size={14} /> Export Sheet
          </button>
        </div>

        {/* ROWS TABLE — current row pointer + per-row status */}
        {batch.rows.length > 0 && (
          <div ref={batchTableBodyRef} style={{ maxHeight: 280, overflowY: 'auto', border: '0.5px solid var(--border)', borderRadius: 8 }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
              <thead style={{ position: 'sticky', top: 0, background: 'var(--bg3)', zIndex: 1 }}>
                <tr>
                  <th style={{ padding: '6px 8px', textAlign: 'left', width: 24 }}></th>
                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>#</th>
                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Phone</th>
                  {batch.nameKey && <th style={{ padding: '6px 8px', textAlign: 'left' }}>Name</th>}
                  <th style={{ padding: '6px 8px', textAlign: 'left' }}>Status</th>
                </tr>
              </thead>
              <tbody>
                {(() => { var firstActiveIdx = batch.activeIndexes.size ? Math.min(...batch.activeIndexes) : -1; return batch.rows.map((r, i) => (
                  <tr
                    key={i}
                    ref={i === firstActiveIdx ? activeRowRef : null}
                    style={{ background: batch.activeIndexes.has(i) ? 'var(--bg3)' : 'transparent' }}
                  >
                    <td style={{ padding: '6px 8px' }}>{batch.activeIndexes.has(i) && <ArrowRight size={13} color="var(--accent)" />}</td>
                    <td style={{ padding: '6px 8px', color: 'var(--text3)' }}>{r.__row}</td>
                    <td style={{ padding: '6px 8px', fontFamily: 'monospace', color: 'var(--text1)' }}>{toE164(r.__phone, batch.countryCode)}</td>
                    {batch.nameKey && <td style={{ padding: '6px 8px', color: 'var(--text2)' }}>{r[batch.nameKey] || '—'}</td>}
                    <td style={{ padding: '6px 8px' }}>
                      <span style={{ color: batchStatusColor(r.__status), fontWeight: 600 }}>
                        {r.__status}
                      </span>
                    </td>
                  </tr>
                )) })()}
              </tbody>
            </table>
          </div>
        )}

        {/* CURRENT-ROW LOG — single line, not a growing list */}
        {batch.currentLog && (
          <div style={{ display: 'flex', gap: 8, fontSize: 11, padding: '6px 8px', background: 'var(--bg3)', borderRadius: 6, alignItems: 'center' }}>
            <span style={{ color: 'var(--text3)', flexShrink: 0 }}>{batch.currentLog.time.toLocaleTimeString()}</span>
            <span style={{ color: 'var(--text3)', flexShrink: 0 }}>row {batch.currentLog.row}</span>
            <span style={{ color: batch.currentLog.level === 'err' ? 'var(--hot)' : batch.currentLog.level === 'ok' ? '#4ade80' : 'var(--text1)' }}>{batch.currentLog.message}</span>
          </div>
        )}
      </div>

      {/* PROFILE SELECTOR */}
      <div style={{ background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 12, padding: 16, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5 }}>
            Profile
          </span>

          <select
            value={selectedId}
            onChange={e => setSelectedId(e.target.value)}
            disabled={agentsLoading}
            style={{ ...inputStyle, width: 'auto', minWidth: 220 }}
          >
            {agentsLoading && <option>Loading…</option>}
            {!agentsLoading && agents.length === 0 && <option>No agents found</option>}
            {agents.map(a => (
              <option key={a.agent_id} value={a.agent_id}>
                {a.is_active === false ? '○ ' : '● '}{a.name || a.agent_id}
              </option>
            ))}
          </select>

          <div style={{ flex: 1 }} />

          <button
            onClick={() => setShowCreate(p => !p)}
            style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '6px 12px', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--text2)', fontSize: 12, fontWeight: 600, cursor: 'pointer' }}
          >
            <Plus size={13} /> New Agent
          </button>

          {selectedId && selectedId !== 'default' && (
            <button
              onClick={deleteAgent}
              style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '6px 10px', background: 'none', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--hot)', fontSize: 12, cursor: 'pointer' }}
            >
              <Trash2 size={13} />
            </button>
          )}
        </div>

        {!agentsLoading && agents.length === 0 && (
          <p style={{ fontSize: 12, color: 'var(--hot)', margin: 0 }}>
            No agents found in the database. Check that final_schema.sql has been run against this
            Supabase project, and that this browser can reach it (check .env / VITE_SUPABASE_URL).
          </p>
        )}

        {selectedId && (
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 24, flexWrap: 'wrap' }}>
            <div>
              <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
                Name
              </label>
              <input
                value={name}
                onChange={e => setName(e.target.value)}
                onBlur={e => saveField('name', e.target.value.trim())}
                placeholder="e.g. Alex"
                style={{ ...inputStyle, maxWidth: 320 }}
              />
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
                Phone Number
              </label>
              <input
                value={phoneNumber}
                onChange={e => setPhoneNumber(e.target.value)}
                onBlur={e => saveField('phone_number', e.target.value.trim())}
                placeholder={
                  plivoNumbers.find(n => n.assigned_agent_id === selectedId)?.number
                    ? `Linked: ${plivoNumbers.find(n => n.assigned_agent_id === selectedId).number}`
                    : 'e.g. +14155550123'
                }
                style={{ ...inputStyle, maxWidth: 220 }}
              />
            </div>

            <div>
              <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6 }}>
                Call Pool
              </label>
              <button
                onClick={() => toggleAgentActive(!isActive)}
                disabled={togglingActive}
                className={isActive ? styles.filterActive : ''}
                style={{
                  display: 'flex', alignItems: 'center', gap: 6, padding: '7px 14px',
                  borderRadius: 20, border: '0.5px solid var(--border)',
                  background: isActive ? undefined : 'transparent',
                  color: isActive ? undefined : 'var(--text2)',
                  fontSize: 12, fontWeight: 600, cursor: 'pointer',
                  opacity: togglingActive ? 0.6 : 1,
                }}
              >
                <span style={{ width: 7, height: 7, borderRadius: '50%', background: isActive ? '#4ade80' : 'var(--text3)' }} />
                {isActive ? 'Active' : 'Inactive'}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* CREATE FORM — sits right under Profile so the flow reads:
          Profile + settings → Create form → Plivo numbers → Prompt */}
      {showCreate && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, background: 'var(--bg2)', border: '0.5px solid var(--accent)', borderRadius: 12, padding: 14 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
            <input value={newId} onChange={e => setNewId(e.target.value)} placeholder="agent_id (e.g. alex)" style={inputStyle} />
            <input value={newName} onChange={e => setNewName(e.target.value)} placeholder="Display name" style={inputStyle} />
          </div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button onClick={() => setShowCreate(false)} style={{ padding: '7px 12px', background: 'transparent', border: 'none', color: 'var(--text2)', fontSize: 12, cursor: 'pointer' }}>
              Cancel
            </button>
            <button
              onClick={createAgent}
              disabled={creating}
              style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '7px 14px', background: 'var(--accent)', border: 'none', borderRadius: 7, color: '#fff', fontSize: 12, fontWeight: 600, cursor: 'pointer', opacity: creating ? 0.6 : 1 }}
            >
              <Plus size={13} /> {creating ? 'Creating…' : 'Create Agent'}
            </button>
          </div>
        </div>
      )}

      {/* PLIVO NUMBERS CARD */}
      <div style={{ background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 12, padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5 }}>
            Plivo Numbers
          </span>
          <div style={{ flex: 1 }} />
          <button
            onClick={loadPlivoNumbers}
            disabled={plivoLoading}
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 12px', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--text1)', fontSize: 12, cursor: 'pointer', opacity: plivoLoading ? 0.6 : 1 }}
          >
            <RefreshCw size={13} className={plivoLoading ? styles.spin : ''} /> Refresh
          </button>
        </div>

        {plivoError && (
          <p style={{ fontSize: 12, color: 'var(--hot)', margin: 0 }}>{plivoError}</p>
        )}

        {!plivoError && plivoLoading && plivoNumbers.length === 0 && (
          <p style={{ fontSize: 12, color: 'var(--text3)', margin: 0 }}>Loading numbers…</p>
        )}

        {!plivoLoading && !plivoError && plivoNumbers.length === 0 && (
          <p style={{ fontSize: 12, color: 'var(--text3)', margin: 0 }}>No numbers found on this Plivo account.</p>
        )}

        {plivoNumbers.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {plivoNumbers.map(n => (
              <div key={n.number} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '8px 10px', background: 'var(--bg3)', borderRadius: 8, fontSize: 13 }}>
                <span style={{ fontFamily: 'monospace', color: 'var(--text1)', minWidth: 140 }}>{n.number}</span>
                <span style={{ fontSize: 11, color: 'var(--text3)' }}>{n.region || '—'}</span>
                <div style={{ flex: 1 }} />
                <span style={{ fontSize: 11, color: n.assigned_agent_name ? 'var(--accent)' : 'var(--text3)' }}>
                  {n.assigned_agent_name ? `→ ${n.assigned_agent_name}` : 'Unassigned'}
                </span>
                {selectedId && n.assigned_agent_id !== selectedId && (
                  <button
                    onClick={() => linkNumberToSelected(n.number, n.region)}
                    disabled={linkingNumber === n.number}
                    style={{ padding: '5px 10px', background: 'var(--accent)', border: 'none', borderRadius: 6, color: '#fff', fontSize: 11, fontWeight: 600, cursor: 'pointer', opacity: linkingNumber === n.number ? 0.6 : 1 }}
                  >
                    {linkingNumber === n.number ? '…' : `Assign to ${name || selectedId}`}
                  </button>
                )}
                {n.assigned_agent_id === selectedId && (
                  <button
                    onClick={() => unlinkNumber(n.number)}
                    disabled={linkingNumber === n.number}
                    style={{ padding: '5px 10px', background: 'transparent', border: '0.5px solid var(--border)', borderRadius: 6, color: 'var(--hot)', fontSize: 11, cursor: 'pointer', opacity: linkingNumber === n.number ? 0.6 : 1 }}
                  >
                    Unlink
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* PROMPTS LABEL */}
      {selectedId && (
        <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5 }}>
          Prompts for agent and prompt history
        </span>
      )}

      {/* SUGGESTED PROMPTS */}
      {selectedId && (
        <div style={{ marginBottom: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
            <Sparkles size={13} color="var(--accent)" />
            <span style={{ fontSize: 11, color: 'var(--text2)' }}>
              Not sure what to write? Start from a template — you can edit it after.
            </span>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {PROMPT_TEMPLATES.map(tpl => (
              <button
                key={tpl.id}
                title={tpl.description}
                onClick={() => {
                  if (prompt.trim() && !window.confirm(`Replace the current prompt with the "${tpl.label}" template?`)) return
                  setPrompt(tpl.text)
                  setDirty(true)
                }}
                style={{
                  padding: '8px 12px', background: 'var(--bg2)', border: '0.5px solid var(--border2)',
                  borderRadius: 8, color: 'var(--text1)', fontSize: 12, fontWeight: 500,
                  cursor: 'pointer', textAlign: 'left',
                }}
              >
                {tpl.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* PROMPT EDITOR */}
      {selectedId && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 280px', gap: 20 }}>
          <div>
            <textarea
              value={prompt}
              onChange={e => { setPrompt(e.target.value); setDirty(true) }}
              disabled={promptLoading}
              placeholder={promptLoading ? 'Loading prompt…' : 'System prompt for this agent…'}
              style={{
                width: '100%', minHeight: 420, background: 'var(--bg2)', border: '0.5px solid var(--border)',
                borderRadius: 10, padding: 14, color: 'var(--text1)', fontSize: 13, fontFamily: 'monospace',
                lineHeight: 1.5, resize: 'vertical', outline: 'none',
              }}
            />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 10 }}>
              <button
                onClick={savePrompt}
                disabled={!dirty || savingPrompt || promptLoading}
                style={{
                  display: 'flex', alignItems: 'center', gap: 6, padding: '8px 16px',
                  background: dirty ? 'var(--accent)' : 'var(--bg3)', border: 'none', borderRadius: 8,
                  color: dirty ? '#fff' : 'var(--text3)', fontSize: 13, fontWeight: 600,
                  cursor: dirty ? 'pointer' : 'default', opacity: savingPrompt ? 0.6 : 1,
                }}
              >
                <Save size={14} /> {savingPrompt ? 'Saving…' : 'Save Prompt'}
              </button>
              <span style={{ fontSize: 11, color: 'var(--text3)' }}>
                {Math.round((prompt || '').length / 4)} tokens (approx)
              </span>
            </div>
          </div>

          <div>
            <h3 style={{ fontSize: 12, fontWeight: 600, color: 'var(--text2)', textTransform: 'uppercase', letterSpacing: 0.5, margin: '0 0 10px 0' }}>
              History
            </h3>
            {rollback.length === 0 && (
              <p style={{ fontSize: 12, color: 'var(--text3)' }}>No saved versions yet.</p>
            )}
            {rollback.map(log => (
              <div
                key={log.id}
                onMouseEnter={() => setHoveredLogId(log.id)}
                onMouseLeave={() => setHoveredLogId(null)}
                style={{ position: 'relative', background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 8, padding: 10, marginBottom: 8, cursor: 'default' }}
              >
                <p style={{ color: 'var(--text3)', margin: '0 0 4px 0', fontSize: 11 }}>
                  {new Date(log.created_at).toLocaleString()}
                </p>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                  <button
                    onClick={() => rollbackTo(log.prompt_value)}
                    style={{ background: 'transparent', border: 'none', padding: 0, color: 'var(--accent)', cursor: 'pointer', fontSize: 11, fontWeight: 500 }}
                  >
                    Load this version →
                  </button>
                  <button
                    onClick={() => deleteHistoryEntry(log.id)}
                    title="Delete this version"
                    style={{ background: 'transparent', border: 'none', padding: 2, color: 'var(--text3)', cursor: 'pointer', display: 'flex' }}
                  >
                    <Trash2 size={12} />
                  </button>
                </div>

                {hoveredLogId === log.id && (
                  <div
                    style={{
                      position: 'absolute', right: '100%', top: 0, marginRight: 10,
                      width: 380, maxHeight: 320, overflowY: 'auto',
                      background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8,
                      padding: 12, boxShadow: '0 8px 24px rgba(0,0,0,0.45)', zIndex: 100,
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                      fontSize: 11, fontFamily: 'monospace', lineHeight: 1.5,
                      color: 'var(--text1)', pointerEvents: 'none',
                    }}
                  >
                    {log.prompt_value || '(empty prompt)'}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}