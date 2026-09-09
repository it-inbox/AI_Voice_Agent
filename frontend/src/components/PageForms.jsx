// src/components/PageForms.jsx
// Forms page — Backend email send (Resend) + setup/library modals + sent-log tab.
// Kept as a single file (modals included) per "no further split" rule.
import { useEffect, useState, useMemo } from 'react'
import {
  Download, Send, Copy, CheckCircle, AlertCircle, Clock,
} from 'lucide-react'
import { supabase } from '../supabaseClient'
import styles from './Dashboard.module.css'
import { fmtDate, VisualEmptyState, Badge, exportCSV } from './dashboardShared'

const API_BASE = (typeof window !== 'undefined' && window.__API_BASE__)
  || import.meta.env.VITE_API_BASE
  || 'http://localhost:8000'

const inputStyle = {
  width: '100%', padding: '9px 12px', borderRadius: 8,
  border: '0.5px solid var(--border2)', background: 'var(--bg3)',
  color: 'var(--text1)', fontSize: 13, boxSizing: 'border-box',
  outline: 'none'
}
const labelStyle = { fontSize: 11, color: 'var(--text2)', marginBottom: 4, display: 'block' }

// form_submissions has no real FK to calls (no shared unique key), so a
// PostgREST embed (`select('*, calls(...)')`) isn't possible. Match the
// most recent call for each submission's to_number in JS instead — same
// info the UI wants (lead_category / lead_score), just fetched separately.
async function attachCallInfo(rows) {
  const numbers = [...new Set(rows.map(r => r.to_number).filter(Boolean))]
  if (!numbers.length) return rows

  const { data: calls, error } = await supabase
    .from('calls')
    .select('to_number, lead_category, lead_score, created_at')
    .in('to_number', numbers)
    .order('created_at', { ascending: false })
  if (error || !calls) return rows

  const latestByNumber = new Map()
  for (const c of calls) {
    if (!latestByNumber.has(c.to_number)) latestByNumber.set(c.to_number, c)
  }
  return rows.map(r => ({ ...r, calls: latestByNumber.get(r.to_number) || null }))
}

async function sendFormEmail(to, name, formUrl, { leadId, force } = {}) {
  const res = await fetch(`${API_BASE}/api/send-form-email`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lead_email: to, lead_name: name, form_url: formUrl, lead_id: leadId, force: !!force })
  })
  if (!res.ok) {
    let detail = 'Send failed'
    try { detail = (await res.json()).detail || detail } catch { }
    const err = new Error(detail)
    err.status = res.status
    throw err
  }
  return await res.json()
}

// Page-size choices for the "N entries" selector — keeps the Supabase
// fetch to exactly what's shown instead of always pulling a flat 200.
const PAGE_SIZE_OPTIONS = [10, 20, 30]

function PageSizeControl({ page, setPage, pageSize, setPageSize, total, loading }) {
  const from = total === 0 ? 0 : page * pageSize + 1
  const to = Math.min(total, (page + 1) * pageSize)
  const canPrev = page > 0
  const canNext = to < total
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: 'var(--text2)' }}>
      <span>Show</span>
      <select value={pageSize} onChange={e => { setPageSize(Number(e.target.value)); setPage(0) }}
        style={{
          padding: '4px 8px', borderRadius: 6, border: '0.5px solid var(--border2)',
          background: 'var(--bg3)', color: 'var(--text1)', fontSize: 12
        }}>
        {PAGE_SIZE_OPTIONS.map(n => <option key={n} value={n}>{n}</option>)}
      </select>
      <span style={{ whiteSpace: 'nowrap' }}>{loading ? 'Loading…' : total === 0 ? '0 of 0' : `${from}–${to} of ${total}`}</span>
      <button onClick={() => setPage(p => Math.max(0, p - 1))} disabled={!canPrev}
        style={{
          padding: '3px 10px', borderRadius: 6, border: '0.5px solid var(--border2)',
          background: 'var(--bg3)', color: 'var(--text1)', fontSize: 12,
          cursor: canPrev ? 'pointer' : 'not-allowed', opacity: canPrev ? 1 : 0.4
        }}>‹</button>
      <button onClick={() => setPage(p => p + 1)} disabled={!canNext}
        style={{
          padding: '3px 10px', borderRadius: 6, border: '0.5px solid var(--border2)',
          background: 'var(--bg3)', color: 'var(--text1)', fontSize: 12,
          cursor: canNext ? 'pointer' : 'not-allowed', opacity: canNext ? 1 : 0.4
        }}>›</button>
    </div>
  )
}

function FormSetupModal({ onClose, onSave, showToast }) {
  const [formUrl, setFormUrl] = useState(localStorage.getItem('google_form_url') || '')
  const [label, setLabel] = useState('')
  const [saving, setSaving] = useState(false)

  async function handleSave() {
    if (!formUrl.includes('docs.google.com/forms')) {
      showToast('Paste a valid Google Form URL'); return
    }

    // BUG FIX: upsert-by-URL used to silently rename an existing library
    // entry if the same URL was re-saved under a different label. Check
    // first and confirm before clobbering someone else's naming.
    const { data: existing } = await supabase.from('forms').select('label').eq('form_url', formUrl).maybeSingle()
    const newLabel = label.trim() || `Form ${new Date().toLocaleDateString()}`
    if (existing && existing.label && existing.label !== newLabel) {
      const ok = window.confirm(`This URL is already saved as "${existing.label}". Overwrite the label with "${newLabel}"?`)
      if (!ok) return
    }

    setSaving(true)
    const { error } = await supabase.from('forms').upsert({
      form_url: formUrl,
      label: newLabel,
      created_by: 'dashboard',
      last_used_at: new Date().toISOString()
    }, { onConflict: 'form_url' })

    setSaving(false)

    if (error) { showToast('Save failed: ' + error.message); return }

    localStorage.setItem('google_form_url', formUrl)
    onSave(formUrl)
    showToast('Form saved!')
    onClose()
  }

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ background: 'var(--bg2)', border: '0.5px solid var(--border2)', borderRadius: 12, padding: 28, width: 440, display: 'flex', flexDirection: 'column', gap: 16 }}>
        <h3 style={{ margin: 0, fontSize: 15, color: 'var(--text1)' }}>Google Form Setup</h3>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <label style={labelStyle}>Form Name (optional)</label>
          <input value={label} onChange={e => setLabel(e.target.value)}
            placeholder="e.g. Onboarding Form, Discovery Call" style={inputStyle} />
        </div>

        <button onClick={() => window.open('https://docs.google.com/forms/create', '_blank')}
          style={{
            padding: '9px 14px', borderRadius: 8, border: '0.5px solid var(--border2)',
            background: 'var(--bg3)', color: 'var(--text1)', fontSize: 13, cursor: 'pointer',
            display: 'flex', alignItems: 'center', gap: 8
          }}>
          ➕ Create New Google Form
          <span style={{ fontSize: 11, color: 'var(--text3)' }}>(opens in new tab)</span>
        </button>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <label style={labelStyle}>Paste Form Share URL *</label>
          <input value={formUrl} onChange={e => setFormUrl(e.target.value)}
            placeholder="https://docs.google.com/forms/d/e/..." style={inputStyle} />
          <span style={{ fontSize: 10, color: 'var(--text3)' }}>Google Forms → Share → Copy link → paste here</span>
        </div>

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button onClick={onClose}
            style={{
              padding: '7px 16px', borderRadius: 8, border: '0.5px solid var(--border2)',
              background: 'var(--bg3)', color: 'var(--text2)', fontSize: 13, cursor: 'pointer'
            }}>
            Cancel
          </button>
          <button onClick={handleSave} disabled={!formUrl || saving}
            style={{
              padding: '7px 16px', borderRadius: 8, border: 'none',
              background: 'var(--accent)', color: '#fff', fontSize: 13, cursor: (!formUrl || saving) ? 'not-allowed' : 'pointer',
              opacity: (!formUrl || saving) ? 0.5 : 1
            }}>
            {saving ? 'Saving…' : 'Save & Use'}
          </button>
        </div>
      </div>
    </div>
  )
}

function FormLibraryModal({ onClose, onUse, showToast }) {
  const [forms, setForms] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    supabase.from('forms')
      .select('*')
      .order('last_used_at', { ascending: false, nullsLast: true })
      .then(({ data, error }) => {
        if (error) showToast('Load failed: ' + error.message)
        setForms(data || [])
        setLoading(false)
      })
  }, [])

  async function deleteForm(id) {
    const { error } = await supabase.from('forms').delete().eq('id', id)
    if (error) { showToast('Delete failed'); return }
    setForms(f => f.filter(x => x.id !== id))
    showToast('Form removed')
  }

  async function useForm(form) {
    await supabase.from('forms')
      .update({ last_used_at: new Date().toISOString() })
      .eq('id', form.id)
    localStorage.setItem('google_form_url', form.form_url)
    onUse(form.form_url)
    showToast('Now using: ' + form.label)
    onClose()
  }

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ background: 'var(--bg2)', border: '0.5px solid var(--border2)', borderRadius: 12, padding: 28, width: 520, display: 'flex', flexDirection: 'column', gap: 16, maxHeight: '80vh' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3 style={{ margin: 0, fontSize: 15, color: 'var(--text1)' }}>Forms Library</h3>
          <button onClick={onClose}
            style={{
              padding: '4px 12px', borderRadius: 7, border: '0.5px solid var(--border2)',
              background: 'var(--bg3)', color: 'var(--text2)', fontSize: 12, cursor: 'pointer'
            }}>
            Close
          </button>
        </div>

        {loading ? (
          <div style={{ textAlign: 'center', padding: '32px 0', color: 'var(--text3)', fontSize: 13 }}>
            Loading…
          </div>
        ) : forms.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '32px 0', color: 'var(--text3)', fontSize: 13 }}>
            No saved forms. Use ⚙️ Setup Form to add one.
          </div>
        ) : (
          <div style={{ overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 10 }}>
            {forms.map(f => (
              <div key={f.id}
                style={{
                  background: 'var(--bg3)', border: '0.5px solid var(--border2)',
                  borderRadius: 10, padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 8
                }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <div>
                    <div style={{ fontWeight: 600, fontSize: 13, color: 'var(--text1)' }}>{f.label}</div>
                    {f.created_by && <div style={{ fontSize: 11, color: 'var(--text3)', marginTop: 2 }}>Added by {f.created_by}</div>}
                    <div style={{ fontSize: 11, color: 'var(--text3)', marginTop: 2 }}>{fmtDate(f.created_at)}</div>
                  </div>
                </div>
                <div style={{ fontSize: 11, color: 'var(--text3)', wordBreak: 'break-all' }}>{f.form_url}</div>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  <button onClick={() => window.open(f.form_url, '_blank')}
                    style={{
                      padding: '5px 12px', borderRadius: 7, border: '0.5px solid var(--border2)',
                      background: 'var(--bg2)', color: 'var(--text1)', fontSize: 12, cursor: 'pointer'
                    }}>
                    🔗 Open
                  </button>
                  <button onClick={() => useForm(f)}
                    style={{
                      padding: '5px 12px', borderRadius: 7, border: 'none',
                      background: 'var(--accent)', color: '#fff', fontSize: 12, cursor: 'pointer'
                    }}>
                    ✓ Use Form
                  </button>
                  <button onClick={() => { navigator.clipboard.writeText(f.form_url); showToast('URL copied') }}
                    style={{
                      padding: '5px 12px', borderRadius: 7, border: '0.5px solid var(--border2)',
                      background: 'var(--bg2)', color: 'var(--text2)', fontSize: 12, cursor: 'pointer'
                    }}>
                    <Copy size={12} />
                  </button>
                  <button onClick={() => deleteForm(f.id)}
                    style={{
                      padding: '5px 12px', borderRadius: 7, border: '0.5px solid rgba(239,68,68,0.3)',
                      background: 'rgba(239,68,68,0.08)', color: '#ef4444', fontSize: 12, cursor: 'pointer'
                    }}>
                    Delete
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function SendFormModal({ onClose, onSent, showToast, formUrl }) {
  const [name, setName] = useState('')
  const [leadEmail, setLeadEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)

  function isValidEmail(e) { return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e) }

  async function handleSend() {
    if (!leadEmail.trim()) { showToast('Email is required'); return }
    if (!isValidEmail(leadEmail)) { showToast('Enter a valid email'); return }
    if (!formUrl) { showToast('No form URL — click ⚙️ Setup Form'); return }

    setBusy(true)
    try {
      await sendFormEmail(leadEmail, name, formUrl)
      setSent(true)
      showToast(`Email sent to ${leadEmail}`)
      setTimeout(() => { onSent(); onClose() }, 1000)
    } catch (err) {
      showToast(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.5)', zIndex: 999, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ background: 'var(--bg2)', border: '0.5px solid var(--border2)', borderRadius: 12, padding: 28, width: 520, display: 'flex', flexDirection: 'column', gap: 18 }}>
        <h3 style={{ margin: 0, fontSize: 15, color: 'var(--text1)' }}>Send Form via Email</h3>

        {!formUrl && (
          <div style={{
            padding: '10px 14px', borderRadius: 8, background: 'rgba(239,68,68,0.1)',
            border: '0.5px solid rgba(239,68,68,0.3)', color: '#ef4444', fontSize: 12
          }}>
            ⚠️ No form URL. Close and click ⚙️ Setup Form.
          </div>
        )}

        {[
          { label: 'Lead Email *', val: leadEmail, set: setLeadEmail, ph: 'lead@example.com', type: 'email' },
          { label: 'Lead Name', val: name, set: setName, ph: 'Full name', type: 'text' },
        ].map(({ label, val, set, ph, type }) => (
          <div key={label} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label style={labelStyle}>{label}</label>
            <input type={type} value={val} onChange={e => set(e.target.value)}
              placeholder={ph} style={inputStyle} />
          </div>
        ))}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', paddingTop: 4 }}>
          <button onClick={onClose}
            style={{
              padding: '8px 18px', borderRadius: 8, border: '0.5px solid var(--border2)',
              background: 'var(--bg3)', color: 'var(--text2)', fontSize: 13, cursor: 'pointer'
            }}>
            Cancel
          </button>
          <button onClick={handleSend}
            disabled={busy || !leadEmail || !formUrl}
            style={{
              padding: '8px 20px', borderRadius: 8, border: 'none', fontSize: 13,
              background: sent ? '#22c55e' : 'var(--accent)', color: '#fff', cursor: 'pointer',
              display: 'flex', alignItems: 'center', gap: 6, minWidth: 130, justifyContent: 'center',
              opacity: (busy || !leadEmail || !formUrl) ? 0.5 : 1
            }}>
            {sent ? '✓ Sent!' : busy ? 'Sending…' : '📧 Send Email'}
          </button>
        </div>
      </div>
    </div>
  )
}

function SendLogTab({ showToast, formUrl, lastSeenResponsesAt }) {
  const [log, setLog] = useState([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(0)
  // Fix: page size used to live only in component state, so switching
  // away from Sent Log — or navigating to another page entirely, which
  // unmounts this component — reset it back to the 20 default every
  // time. Persist the choice so it's remembered like the rest of the UI.
  const [pageSize, setPageSizeRaw] = useState(() => Number(localStorage.getItem('forms_sent_log_page_size')) || 20)
  function setPageSize(n) { localStorage.setItem('forms_sent_log_page_size', String(n)); setPageSizeRaw(n) }
  const [total, setTotal] = useState(0)
  const [resendingId, setResendingId] = useState(null)
  // BUG FIX: this used to receive `submissions` from the parent's
  // Responses-tab state — but once Responses got its own pagination,
  // that was only ever the 10/20/30 rows currently visible on THAT
  // tab, so status lookups here silently broke for anything not on
  // that page. The Sent Log now fetches exactly the submissions it
  // needs for the rows it's showing, independent of the other tab.
  const [linkedSubmissions, setLinkedSubmissions] = useState([])

  async function loadLog() {
    setLoading(true)
    const from = page * pageSize, to = from + pageSize - 1
    const { data, count } = await supabase.from('form_send_log')
      .select('*', { count: 'exact' })
      .order('sent_at', { ascending: false })
      .range(from, to)

    const rows = data || []
    setTotal(count || 0)

    const leadIds = [...new Set(rows.map(r => r.lead_id).filter(Boolean))]
    const emails = [...new Set(rows.filter(r => !r.lead_id).map(r => (r.lead_email || '').trim().toLowerCase()).filter(Boolean))]
    const linked = []
    if (leadIds.length) {
      const { data: byId } = await supabase.from('form_submissions').select('*').in('id', leadIds)
      linked.push(...(byId || []))
    }
    if (emails.length) {
      const { data: byEmail } = await supabase.from('form_submissions').select('*').in('email', emails)
      linked.push(...(byEmail || []))
    }
    setLinkedSubmissions(linked)
    setLog(rows)
    setLoading(false)
  }

  useEffect(() => { loadLog() }, [page, pageSize])

  const submissionsByEmail = useMemo(() => {
    const map = new Map()
    linkedSubmissions.forEach(s => {
      const email = (s.email || '').trim().toLowerCase()
      if (!email) return
      const existing = map.get(email)
      if (!existing || new Date(s.submitted_at) > new Date(existing.submitted_at)) map.set(email, s)
    })
    return map
  }, [linkedSubmissions])

  const submissionsById = useMemo(() => {
    const map = new Map()
    linkedSubmissions.forEach(s => map.set(s.id, s))
    return map
  }, [linkedSubmissions])

  async function handleResend(l) {
    const url = l.form_url || formUrl
    if (!url) { showToast('No form URL available'); return }
    setResendingId(l.id)
    try {
      // force: true — a deliberate Resend click should bypass the
      // server-side cooldown meant to catch accidental double-sends.
      await sendFormEmail(l.lead_email, l.lead_name, url, { leadId: l.lead_id, force: true })
      showToast(`Reminder sent to ${l.lead_email}`)
      loadLog()
    } catch (err) {
      showToast('Resend failed: ' + err.message)
    } finally {
      setResendingId(null)
    }
  }

  // Fix: this used to fuzzy-match by email + "submitted after sent_at"
  // across whatever rows happened to be loaded on both sides — two
  // leads sharing an inbox could cross-match. Prefer the real lead_id
  // link written by the backend now; only fall back to the fuzzy
  // match for older log rows sent before that column existed.
  function findResponse(l) {
    if (l.lead_id) return submissionsById.get(l.lead_id) || null
    const email = (l.lead_email || '').trim().toLowerCase()
    if (!email) return null
    const candidate = submissionsByEmail.get(email)
    if (candidate && new Date(candidate.submitted_at) >= new Date(l.sent_at)) return candidate
    return null
  }

  // "Most recently replied should be first": entries that have a
  // response bubble to the top, newest reply first; entries still
  // pending stay below in sent-time order. This resort only covers
  // the current page — true reply-recency ordering *across* pages
  // would need a `responded_at` column written on form_send_log by a
  // DB trigger/webhook when a matching submission comes in, since the
  // response time isn't something Supabase can order by server-side
  // today. Ask if you want that added — it's a schema change plus a
  // Postgres trigger, out of scope for a page size selector.
  const sortedLog = useMemo(() => {
    const responded = [], pending = []
    log.forEach(l => {
      const r = findResponse(l)
      if (r) responded.push({ l, respondedAt: new Date(r.submitted_at).getTime() })
      else pending.push(l)
    })
    responded.sort((a, b) => b.respondedAt - a.respondedAt)
    pending.sort((a, b) => new Date(b.sent_at) - new Date(a.sent_at))
    return [...responded.map(x => x.l), ...pending]
  }, [log, linkedSubmissions])

  function StatusCell({ l }) {
    const responded = findResponse(l)
    if (responded) {
      // New-response tag: distinguishes replies that came in since the
      // admin last opened this tab, so opening Sent Log still shows
      // "which ones are new" even though the sidebar badge clears the
      // moment the tab opens.
      const isNew = lastSeenResponsesAt && new Date(responded.submitted_at) > new Date(lastSeenResponsesAt)
      return (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4, color: '#22c55e', fontSize: 12, fontWeight: isNew ? 700 : 400 }}
          title={`Responded ${fmtDate(responded.submitted_at)}`}>
          <CheckCircle size={13} /> {isNew ? '🆕 New Response' : 'Responded'}
        </span>
      )
    }
    if (l.status === 'failed') {
      return <span style={{ display: 'flex', alignItems: 'center', gap: 4, color: '#ef4444', fontSize: 12 }} title={l.error || ''}><AlertCircle size={13} /> Failed</span>
    }
    return <span style={{ display: 'flex', alignItems: 'center', gap: 4, color: 'var(--text3)', fontSize: 12 }}><Clock size={13} /> Pending</span>
  }

  return (
    <div className={styles.tableCard}>
      <div style={{ display: 'flex', justifyContent: 'flex-end', padding: '10px 14px 0' }}>
        <PageSizeControl page={page} setPage={setPage} pageSize={pageSize} setPageSize={setPageSize} total={total} loading={loading} />
      </div>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Lead Name</th><th>Email</th>
              <th>Sent At</th><th>Status</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={5} className={styles.emptyRow}>Loading…</td></tr>
            ) : sortedLog.length === 0 ? (
              <tr><td colSpan={5}><VisualEmptyState message="No forms sent yet" /></td></tr>
            ) : sortedLog.map(l => (
              <tr key={l.id} className={styles.tableRow}>
                <td style={{ fontWeight: 500 }}>{l.lead_name || '—'}</td>
                <td style={{ fontSize: 12, color: 'var(--text2)' }}>{l.lead_email}</td>
                <td style={{ color: 'var(--text2)', whiteSpace: 'nowrap' }}>{fmtDate(l.sent_at)}</td>
                <td><StatusCell l={l} /></td>
                <td>
                  <div className={styles.actions}>
                    <button className={styles.iconBtn} title="Resend Email" disabled={resendingId === l.id} onClick={() => handleResend(l)}>
                      <Send size={14} />
                    </button>
                    <button className={styles.iconBtn} title="Copy Email"
                      onClick={() => { navigator.clipboard.writeText(l.lead_email); showToast('Email copied') }}>
                      <Copy size={14} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// Sidebar "Forms" badge behavior: it's a notification for NEW
// responses since the admin last opened the Sent Log tab — not a
// running total of all submissions. Persisted per-browser.
const LAST_SEEN_KEY = 'forms_last_seen_responses_at'

export default function PageForms({ showToast, setFormCount }) {
  const [submissions, setSubmissions] = useState([])
  const [loading, setLoading] = useState(true)
  const [fetchError, setFetchError] = useState(null)
  const [tab, setTab] = useState('responses')
  const [showModal, setShowModal] = useState(false)
  const [showSetup, setShowSetup] = useState(false)
  const [showLibrary, setShowLibrary] = useState(false)
  const [formUrl, setFormUrl] = useState(localStorage.getItem('google_form_url') || '')
  const [page, setPage] = useState(0)
  // Fix: same as Sent Log — page size used to reset to 20 whenever this
  // component unmounted (switching to another page in the app and back).
  // Persisted separately from Sent Log's so each tab remembers its own.
  const [pageSize, setPageSizeRaw] = useState(() => Number(localStorage.getItem('forms_responses_page_size')) || 20)
  function setPageSize(n) { localStorage.setItem('forms_responses_page_size', String(n)); setPageSizeRaw(n) }
  const [total, setTotal] = useState(0)
  const [exporting, setExporting] = useState(false)
  // Snapshot of the "last seen" timestamp captured the moment Sent Log
  // is opened, BEFORE it gets overwritten to now(). Used only to tag
  // "🆕 New Response" rows for this viewing — the badge itself clears
  // immediately using the fresh value, independent of this snapshot.
  const [lastSeenSnapshot, setLastSeenSnapshot] = useState(() => localStorage.getItem(LAST_SEEN_KEY))

  function loadUnseenCount() {
    const lastSeen = localStorage.getItem(LAST_SEEN_KEY) || new Date(0).toISOString()
    supabase.from('form_submissions')
      .select('id', { count: 'exact', head: true })
      .gt('submitted_at', lastSeen)
      .then(({ count }) => {
        if (typeof setFormCount === 'function') setFormCount(count || 0)
      })
  }

  useEffect(() => { loadUnseenCount() }, [])

  // Fix: this used to only fire when the Sent Log sub-tab was opened
  // (`tab === 'sent'`), but the Responses tab is the default view and
  // where new submissions are actually seen (rows are on-screen right
  // there). An admin who only ever looks at Responses would never once
  // clear LAST_SEEN_KEY — badge stuck at the total submission count on
  // every load, and the first Sent Log visit would tag ALL of them as
  // "🆕 New Response" even ones seen days ago. Snapshot + clear on Forms
  // page mount instead, regardless of which sub-tab is open — visiting
  // the page at all counts as "seen".
  useEffect(() => {
    setLastSeenSnapshot(localStorage.getItem(LAST_SEEN_KEY) || new Date(0).toISOString())
    localStorage.setItem(LAST_SEEN_KEY, new Date().toISOString())
    if (typeof setFormCount === 'function') setFormCount(0)
  }, [])

  // Live-updates the badge the moment a new form response comes in —
  // without this, the count would only refresh on next page load.
  useEffect(() => {
    const channel = supabase
      .channel('form_submissions_watch')
      .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'form_submissions' }, () => {
        loadUnseenCount()
      })
      .subscribe()
    return () => { channel.unsubscribe() }
  }, [])

  function buildSubmissionsQuery(withCount) {
    let q = supabase.from('form_submissions').select('*', withCount ? { count: 'exact' } : undefined)
    // Most recent submission first — already the default; kept explicit
    // since this is exactly the ordering asked for on the Responses tab.
    return q.order('submitted_at', { ascending: false })
  }

  function loadSubmissions() {
    setLoading(true); setFetchError(null)
    const from = page * pageSize, to = from + pageSize - 1
    buildSubmissionsQuery(true).range(from, to).then(async ({ data, error, count }) => {
      if (error) { console.error('[PageForms]', error.message); setFetchError(error.message) }
      setSubmissions(await attachCallInfo(data || []))
      setTotal(count || 0)
      setLoading(false)
    })
  }

  // Fix: fetch was hardcoded to .limit(200) with no page-size control,
  // always pulling the max regardless of what's shown — this is exactly
  // what the 10/20/30 selector below is for.
  useEffect(() => { loadSubmissions() }, [page, pageSize])

  function handleUseForm(url) {
    localStorage.setItem('google_form_url', url)
    setFormUrl(url)
  }

  // Fix: Export CSV used to silently export only the current in-memory
  // page (200 rows before, now 10/20/30) with no indication that more
  // rows existed. Pull every matching row for the export instead.
  async function handleExportAll() {
    setExporting(true)
    try {
      const { data, error } = await buildSubmissionsQuery(false).limit(5000)
      if (error) { showToast('Export failed: ' + error.message); return }
      const rows = data || []
      exportCSV(rows, ['name', 'email', 'service_requirements', 'budget', 'timeline', 'submitted_at'], 'form_submissions')
      showToast(`Exported ${rows.length} rows`)
    } finally {
      setExporting(false)
    }
  }

  return (
    <>
      {showSetup && <FormSetupModal onClose={() => setShowSetup(false)} onSave={handleUseForm} showToast={showToast} />}
      {showLibrary && <FormLibraryModal onClose={() => setShowLibrary(false)} onUse={handleUseForm} showToast={showToast} />}
      {showModal && <SendFormModal onClose={() => setShowModal(false)} onSent={() => { }} showToast={showToast} formUrl={formUrl} />}

      {fetchError && (
        <div className={styles.errorBanner} style={{ marginBottom: 12 }}>
          <AlertCircle size={14} />
          <span><b>Forms fetch error:</b> {fetchError}</span>
        </div>
      )}

      <div style={{ display: 'flex', gap: 10, marginBottom: 16, justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', gap: 4 }}>
          {[{ key: 'responses', label: 'Responses' }, { key: 'sent', label: 'Sent Log' }].map(t => (
            <button key={t.key} onClick={() => setTab(t.key)}
              style={{
                padding: '6px 14px', borderRadius: 8, fontSize: 12, cursor: 'pointer',
                border: '0.5px solid var(--border2)',
                background: tab === t.key ? 'var(--accent)' : 'var(--bg3)',
                color: tab === t.key ? '#fff' : 'var(--text2)'
              }}>
              {t.label}
            </button>
          ))}
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          {tab === 'responses' && (
            <PageSizeControl page={page} setPage={setPage} pageSize={pageSize} setPageSize={setPageSize} total={total} loading={loading} />
          )}

          <span style={{
            fontSize: 11, padding: '3px 8px', borderRadius: 6,
            background: formUrl ? 'rgba(34,197,94,0.1)' : 'rgba(239,68,68,0.1)',
            color: formUrl ? '#22c55e' : '#ef4444',
            border: `0.5px solid ${formUrl ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)'}`
          }}>
            {formUrl ? '✓ Form set' : '⚠ No form'}
          </span>

          <button onClick={() => setShowSetup(true)}
            style={{
              padding: '6px 14px', borderRadius: 8, border: '0.5px solid var(--border2)',
              background: 'var(--bg3)', color: 'var(--text1)', fontSize: 12, cursor: 'pointer'
            }}>
            ⚙️ Setup Form
          </button>

          <button onClick={() => setShowLibrary(true)}
            style={{
              padding: '6px 14px', borderRadius: 8, border: '0.5px solid var(--border2)',
              background: 'var(--bg3)', color: 'var(--text1)', fontSize: 12, cursor: 'pointer'
            }}>
            📚 Forms Library
          </button>

          <button onClick={() => setShowModal(true)}
            style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '6px 14px',
              background: 'var(--accent)', border: 'none', borderRadius: 8,
              color: '#fff', fontSize: 12, cursor: 'pointer'
            }}>
            <Send size={13} /> Send Email
          </button>

          {tab === 'responses' && (
            <button onClick={handleExportAll} disabled={exporting} style={{
              display: 'flex', alignItems: 'center', gap: 6, padding: '6px 14px',
              background: 'var(--bg3)', border: '0.5px solid var(--border2)', borderRadius: 8,
              color: 'var(--text1)', fontSize: 12, cursor: exporting ? 'not-allowed' : 'pointer',
              opacity: exporting ? 0.6 : 1
            }}>
              <Download size={13} /> {exporting ? 'Exporting…' : `Export CSV (all ${total})`}
            </button>
          )}
        </div>
      </div>

      {tab === 'responses' ? (
        <div className={styles.tableCard}>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Name</th><th>Email</th><th>Service Requirements</th>
                  <th>Budget</th><th>Timeline</th><th>Lead Status</th>
                  <th>Submitted</th><th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={8} className={styles.emptyRow}>Loading…</td></tr>
                ) : submissions.length === 0 ? (
                  <tr><td colSpan={8}><VisualEmptyState message="No form submissions found" /></td></tr>
                ) : submissions.map(s => (
                  <tr key={s.id} className={styles.tableRow}>
                    <td style={{ fontWeight: 500 }}>{s.name || '—'}</td>
                    <td style={{ color: 'var(--text2)', fontSize: 12 }}>{s.email || '—'}</td>
                    <td className={styles.summaryCell}>{s.service_requirements || '—'}</td>
                    <td style={{ color: 'var(--text2)' }}>{s.budget || '—'}</td>
                    <td style={{ color: 'var(--text2)' }}>{s.timeline || '—'}</td>
                    <td>{s.calls
                      ? <Badge category={s.calls.lead_category} />
                      : <span style={{ color: 'var(--text3)', fontSize: 12 }}>—</span>}
                    </td>
                    <td style={{ color: 'var(--text2)', whiteSpace: 'nowrap' }}>{fmtDate(s.submitted_at)}</td>
                    <td>
                      <div className={styles.actions}>
                        <button className={styles.iconBtn} title="Copy Email"
                          onClick={() => { navigator.clipboard.writeText(s.email || ''); showToast('Email copied') }}>
                          <Copy size={14} />
                        </button>
                        <button className={styles.iconBtn} title="Send Form Email"
                          onClick={async () => {
                            if (!formUrl) { showToast('No form URL configured'); return }
                            if (!s.email) { showToast('No email for this lead'); return }
                            try {
                              // lead_id links this send to the submission row for real
                              // later — instead of the old email+timestamp fuzzy match.
                              await sendFormEmail(s.email, s.name, formUrl, { leadId: s.id })
                              showToast(`Form sent to ${s.email}`)
                            } catch (err) {
                              showToast(err.status === 429 ? err.message : 'Send failed: ' + err.message)
                            }
                          }}>
                          <Send size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <SendLogTab showToast={showToast} formUrl={formUrl} lastSeenResponsesAt={lastSeenSnapshot} />
      )}
    </>
  )
}