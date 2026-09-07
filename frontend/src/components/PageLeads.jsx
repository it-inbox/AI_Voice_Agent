// src/components/PageLeads.jsx
import { useEffect, useState, useMemo } from 'react'
import {
  Download, X, ChevronDown, ChevronLeft, ChevronRight, Send, PhoneCall, Handshake, Calendar, Copy,
} from 'lucide-react'
import { supabase } from '../supabaseClient'
import styles from './Dashboard.module.css'
import {
  CATEGORY_COLOR, fmtDate, fmtDuration, fmtDateTime,
  VisualEmptyState, StarScore, Badge, FilterBar, exportCSV,
} from './dashboardShared'

export default function PageLeads({ records, loading, openTranscript, showToast, fetchAll, agentConfig, globalSearch, goToAgentProfiles }) {
  const [catFilter, setCatFilter] = useState('ALL')
  const [sortKey, setSortKey] = useState('timestamp')
  const [sortDir, setSortDir] = useState('desc')

  // Page size persists across page navigation (localStorage) — was
  // resetting to 10 every time the admin left and came back to Leads.
  const [pageSize, setPageSizeState] = useState(() => {
    try { return Number(localStorage.getItem('leads_page_size')) || 10 } catch { return 10 }
  })
  function setPageSize(n) {
    setPageSizeState(n)
    try { localStorage.setItem('leads_page_size', String(n)) } catch {}
    setCurrentPage(1)
  }
  const [currentPage, setCurrentPage] = useState(1)
  const [detail, setDetail] = useState(null)
  const [notes, setNotes] = useState([])
  const [noteInput, setNoteInput] = useState('')
  const [notesLoading, setNotesLoading] = useState(false)
  const [statusSaving, setStatusSaving] = useState(false)
  const [noteAuthor, setNoteAuthor] = useState('Sales Team')

  const calendlyLink = agentConfig?.calendly_link || 'https://calendly.com'

  const filtered = useMemo(() => {
    return records
      .filter(r => catFilter === 'ALL' || r.lead_category === catFilter)
      .filter(r => !globalSearch ||
        (r.to_number || '').includes(globalSearch) ||
        (r.name || '').toLowerCase().includes(globalSearch.toLowerCase()) ||
        (r.summary || '').toLowerCase().includes(globalSearch.toLowerCase()))
      .sort((a, b) => {
        let av = a[sortKey], bv = b[sortKey]
        if (sortKey === 'timestamp') { av = new Date(av || 0); bv = new Date(bv || 0) }
        if (sortKey === 'lead_score') { av = Number(av); bv = Number(bv) }
        return sortDir === 'asc' ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1)
      })
  }, [records, catFilter, globalSearch, sortKey, sortDir])

  const paged = useMemo(() => filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize), [filtered, pageSize, currentPage])
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))

  // Filters/search/sort changing the result set should snap back to page 1.
  useEffect(() => { setCurrentPage(1) }, [catFilter, globalSearch, sortKey, sortDir])
  // If the current page falls off the end (e.g. filtered set shrank), pull it back.
  useEffect(() => { if (currentPage > totalPages) setCurrentPage(totalPages) }, [totalPages, currentPage])

  const pageStart = filtered.length === 0 ? 0 : (currentPage - 1) * pageSize + 1
  const pageEnd = Math.min(currentPage * pageSize, filtered.length)

  function toggleSort(key) {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(key); setSortDir('desc') }
  }

  const SortBtn = ({ k, label }) => (
    <span onClick={() => toggleSort(k)} style={{ cursor: 'pointer', userSelect: 'none' }}>
      {label} {sortKey === k ? (sortDir === 'asc' ? '↑' : '↓') : ''}
    </span>
  )

  async function loadNotes(callSid) {
    if (!callSid) { setNotes([]); return }
    setNotesLoading(true)
    const { data, error } = await supabase.from('lead_notes')
      .select('*')
      .eq('call_sid', callSid)
      .order('created_at', { ascending: true })
    if (error) console.error('[loadNotes]', error.message)
    setNotes(data || [])
    setNotesLoading(false)
  }

  useEffect(() => {
    loadNotes(detail?.call_sid)
  }, [detail?.call_sid])

  async function updateStatus(callSid, newCategory) {
    setStatusSaving(true)
    const now = new Date().toISOString()
    const { error } = await supabase.from('calls')
      .update({ lead_category: newCategory, last_contacted_at: now })
      .eq('call_sid', callSid)
    setStatusSaving(false)
    if (error) { showToast('Error updating status', 'err'); return }
    showToast(`Status → ${newCategory}`)
    setDetail(d => d ? { ...d, lead_category: newCategory, last_contacted_at: now } : d)
    fetchAll()
  }

  async function markFollowUp(callSid) {
    const now = new Date().toISOString()
    const { error } = await supabase.from('calls')
      .update({ last_contacted_at: now })
      .eq('call_sid', callSid)
    if (error) { showToast('Error updating contact time', 'err'); return }
    showToast('Last contacted updated ✓')
    setDetail(d => d ? { ...d, last_contacted_at: now } : d)
    fetchAll()
  }

  // Follow-up Call: stash the number (+ name) for the Agent Profiles page's
  // single-call box to pick up, then jump there — admin just hits Call.
  function callFollowUp(lead) {
    sessionStorage.setItem('pendingCallNumber', lead.to_number || '')
    sessionStorage.setItem('pendingCallName', lead.name || '')
    markFollowUp(lead.call_sid)
    if (goToAgentProfiles) goToAgentProfiles()
    else showToast('Number copied — open Agent Profiles to call')
  }

  async function addNote() {
    if (!noteInput.trim() || !detail?.call_sid) return
    const { error } = await supabase.from('lead_notes').insert({
      call_sid: detail.call_sid,
      note: noteInput.trim(),
      author: noteAuthor.trim() || 'Sales Team',
    })
    if (error) { showToast('Error saving note', 'err'); return }
    setNoteInput('')
    showToast('Note saved')
    loadNotes(detail.call_sid)
  }

  async function deleteNote(noteId) {
    const { error } = await supabase.from('lead_notes').delete().eq('id', noteId)
    if (error) { showToast('Error deleting note', 'err'); return }
    showToast('Note deleted')
    setNotes(prev => prev.filter(n => n.id !== noteId))
  }

  return (
    <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', gap: 10, marginBottom: 14, justifyContent: 'flex-end', alignItems: 'center' }}>
          <FilterBar value={catFilter} onChange={setCatFilter} cats={['ALL', 'HOT', 'WARM', 'COLD', 'CLOSED']} />
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text2)' }}>
            Show
            <select
              value={pageSize}
              onChange={e => setPageSize(Number(e.target.value))}
              style={{ background: 'var(--bg3)', border: '0.5px solid var(--border2)', borderRadius: 8, padding: '6px 10px', color: 'var(--text1)', fontSize: 12, cursor: 'pointer', outline: 'none' }}>
              {[10, 20, 30, 40].map(n => <option key={n} value={n}>{n}</option>)}
            </select>
            <span style={{ whiteSpace: 'nowrap' }}>{pageStart}-{pageEnd} of {filtered.length}</span>
            <button
              onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
              disabled={currentPage <= 1}
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: 26, height: 26, background: 'var(--bg3)', border: '0.5px solid var(--border2)', borderRadius: 6, color: 'var(--text1)', cursor: currentPage <= 1 ? 'default' : 'pointer', opacity: currentPage <= 1 ? 0.4 : 1 }}
            >
              <ChevronLeft size={14} />
            </button>
            <button
              onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
              disabled={currentPage >= totalPages}
              style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: 26, height: 26, background: 'var(--bg3)', border: '0.5px solid var(--border2)', borderRadius: 6, color: 'var(--text1)', cursor: currentPage >= totalPages ? 'default' : 'pointer', opacity: currentPage >= totalPages ? 0.4 : 1 }}
            >
              <ChevronRight size={14} />
            </button>
          </div>
          <button onClick={() => { exportCSV(filtered, ['name', 'to_number', 'lead_category', 'lead_score', 'duration_sec', 'budget', 'decision_makers', 'timestamp', 'last_contacted_at'], 'leads'); showToast(`Exported ${filtered.length} rows`) }}
            style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 14px', background: 'var(--bg3)', border: '0.5px solid var(--border2)', borderRadius: 8, color: 'var(--text1)', fontSize: 12, cursor: 'pointer', whiteSpace: 'nowrap' }}>
            <Download size={13} /> Export CSV
          </button>
        </div>

        <div className={styles.tableCard}>
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th><SortBtn k="name" label="Name" /></th>
                  <th><SortBtn k="to_number" label="Phone" /></th>
                  <th><SortBtn k="lead_category" label="Status" /></th>
                  <th><SortBtn k="lead_score" label="Score" /></th>
                  <th>Budget</th>
                  <th>Call Status</th>
                  <th>Last Contacted</th>
                  <th><SortBtn k="timestamp" label="Date" /></th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={9} className={styles.emptyRow}>Loading…</td></tr>
                ) : filtered.length === 0 ? (
                  <tr><td colSpan={9}><VisualEmptyState message="No matching leads discovered" /></td></tr>
                ) : paged.map(r => (
                  <tr key={r.call_sid} className={styles.tableRow}
                    style={{ cursor: 'pointer', background: detail?.call_sid === r.call_sid ? 'var(--bg3)' : '' }}
                    onClick={() => setDetail(detail?.call_sid === r.call_sid ? null : r)}>
                    <td style={{ fontWeight: 500 }}>{r.name || '—'}</td>
                    <td className={styles.mono}>{r.to_number || '—'}</td>
                    <td><Badge category={r.lead_category} /></td>
                    <td><StarScore score={r.lead_score || 1} /></td>
                    <td style={{ fontWeight: 500, color: 'var(--green)' }}>{r.budget || '—'}</td>
                    <td className={styles.mono} >{r.live_outcome || '—'}</td>
                    <td style={{ color: 'var(--text2)', fontSize: 12 }}>{r.last_contacted_at ? fmtDateTime(r.last_contacted_at) : fmtDateTime(r.timestamp)}</td>
                    <td style={{ color: 'var(--text2)', whiteSpace: 'nowrap' }}>{fmtDate(r.timestamp)}</td>
                    <td onClick={e => e.stopPropagation()}>
                      <div className={styles.actions}>
                        <button className={styles.iconBtn} title="Copy number" onClick={e => { e.stopPropagation(); navigator.clipboard.writeText(r.to_number || ''); showToast(`Copied ${r.to_number}`) }}><Copy size={14} /></button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {detail && (
        <div style={{ width: 300, flexShrink: 0, background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 14, padding: '1rem', fontSize: 13, maxHeight: 'calc(100vh - 120px)', overflowY: 'auto' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <span style={{ fontWeight: 600 }}>Lead detail</span>
            <button className={styles.iconBtn} onClick={() => setDetail(null)}><X size={14} /></button>
          </div>

          <p style={{ fontWeight: 600, fontSize: 15, marginBottom: 2 }}>{detail.name || 'Unknown'}</p>
          <p className={styles.mono} style={{ color: 'var(--text2)', marginBottom: 10 }}>{detail.to_number || '—'}</p>

          <div style={{ background: 'var(--bg3)', borderRadius: 8, padding: '8px 10px', marginBottom: 12, fontSize: 12 }}>
            <p style={{ color: 'var(--text3)', fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 2 }}>Last Contacted</p>
            <p style={{ color: 'var(--text1)' }}>
              {detail.last_contacted_at ? fmtDateTime(detail.last_contacted_at) : fmtDateTime(detail.timestamp)}
            </p>
          </div>

          <p style={{ color: 'var(--text2)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Status</p>
          <div style={{ position: 'relative', marginBottom: 12 }}>
            <select
              value={detail.lead_category || 'COLD'}
              disabled={statusSaving}
              onChange={async e => await updateStatus(detail.call_sid, e.target.value)}
              style={{
                width: '100%', padding: '6px 28px 6px 10px',
                background: 'var(--bg3)', border: '0.5px solid var(--border2)',
                borderRadius: 8, color: CATEGORY_COLOR[detail.lead_category] || 'var(--text1)',
                fontSize: 13, fontWeight: 600, cursor: 'pointer', appearance: 'none', outline: 'none',
              }}>
              <option value="HOT" style={{ color: '#ff6b4a' }}>🔥 HOT</option>
              <option value="WARM" style={{ color: '#f5a623' }}>🌤 WARM</option>
              <option value="COLD" style={{ color: '#5b9cf6' }}>❄️ COLD</option>
              <option value="CLOSED" style={{ color: '#4ade80' }}>🎉 CLOSED</option>
            </select>
            <ChevronDown size={12} style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text2)', pointerEvents: 'none' }} />
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8, marginBottom: 12 }}>
            <div>
              <p style={{ color: 'var(--text2)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Score</p>
              <StarScore score={detail.lead_score || 1} />
            </div>
            <div>
              <p style={{ color: 'var(--text2)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Duration</p>
              <p>{fmtDuration(detail.duration_sec)}</p>
            </div>
          </div>

          {detail.budget && (
            <div style={{ marginBottom: 12 }}>
              <p style={{ color: 'var(--text2)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Budget</p>
              <p style={{ color: 'var(--green)', fontWeight: 600 }}>{detail.budget}</p>
            </div>
          )}

          {detail.summary && (
            <div style={{ marginBottom: 12 }}>
              <p style={{ color: 'var(--text2)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Summary</p>
              <p style={{ lineHeight: 1.6, color: 'var(--text1)', fontSize: 12 }}>{detail.summary}</p>
            </div>
          )}

          {detail.pain_points?.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <p style={{ color: 'var(--text2)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Pain points</p>
              <ul style={{ paddingLeft: 16, color: 'var(--text1)', lineHeight: 1.8, margin: 0, fontSize: 12 }}>
                {detail.pain_points.map((p, i) => <li key={i}>{p}</li>)}
              </ul>
            </div>
          )}

          {detail.decision_makers && (
            <div style={{ marginBottom: 12 }}>
              <p style={{ color: 'var(--text2)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 4 }}>Decision Maker</p>
              <p style={{ fontSize: 12 }}>{detail.decision_makers}</p>
            </div>
          )}

          <p style={{ color: 'var(--text2)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 8 }}>Sales actions</p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 14 }}>
            <button
              onClick={() => callFollowUp(detail)}
              style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--green)', fontSize: 12, cursor: 'pointer', textAlign: 'left' }}>
              <PhoneCall size={13} /> Follow-up Call
            </button>
            {/* Close Deal only makes sense once a lead has warmed up — hidden for COLD.
                A rep can still move COLD -> WARM/HOT via the Status dropdown above,
                which will then reveal this button. */}
            {['WARM', 'HOT', 'CLOSED'].includes(detail.lead_category) && (
              <button
                onClick={async () => { await updateStatus(detail.call_sid, 'CLOSED'); showToast('Deal closed! 🎉') }}
                disabled={detail.lead_category === 'CLOSED' || statusSaving}
                style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', background: detail.lead_category === 'CLOSED' ? 'rgba(74,222,128,0.08)' : 'var(--bg3)', border: `0.5px solid ${detail.lead_category === 'CLOSED' ? 'rgba(74,222,128,0.4)' : 'var(--border)'}`, borderRadius: 8, color: detail.lead_category === 'CLOSED' ? '#4ade80' : 'var(--hot)', fontSize: 12, cursor: detail.lead_category === 'CLOSED' ? 'default' : 'pointer', textAlign: 'left', opacity: detail.lead_category === 'CLOSED' ? 0.7 : 1 }}>
                <Handshake size={13} /> {detail.lead_category === 'CLOSED' ? 'Deal Closed ✓' : 'Close Deal'}
              </button>
            )}
            <button
              onClick={() => {
                window.open(calendlyLink || 'https://calendly.com', '_blank')
              }}
              style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, color: 'var(--accent)', fontSize: 12, cursor: 'pointer', textAlign: 'left' }}>
              <Calendar size={13} /> Schedule Meeting
            </button>
          </div>

          <p style={{ color: 'var(--text2)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 8 }}>Notes & activity</p>

          <input
            value={noteAuthor}
            onChange={e => setNoteAuthor(e.target.value)}
            placeholder="Your name…"
            style={{ width: '100%', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, padding: '5px 10px', color: 'var(--text2)', fontSize: 11, outline: 'none', marginBottom: 6, boxSizing: 'border-box' }}
          />

          {notesLoading ? (
            <p style={{ color: 'var(--text3)', fontSize: 12, marginBottom: 8 }}>Loading…</p>
          ) : notes.length === 0 ? (
            <p style={{ color: 'var(--text3)', fontSize: 12, marginBottom: 8 }}>No notes yet. Add one below.</p>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 10 }}>
              {notes.map(n => (
                <div key={n.id} style={{ background: 'var(--bg3)', borderRadius: 8, padding: '8px 10px', fontSize: 12, position: 'relative' }}>
                  <p style={{ color: 'var(--text1)', marginBottom: 2, paddingRight: 20 }}>{n.note}</p>
                  <p style={{ color: 'var(--text3)', fontSize: 10 }}>{n.author || 'Sales Team'} · {fmtDateTime(n.created_at)}</p>
                  <button
                    onClick={() => deleteNote(n.id)}
                    style={{ position: 'absolute', top: 6, right: 6, background: 'transparent', border: 'none', cursor: 'pointer', color: 'var(--text3)', padding: 2, display: 'flex', alignItems: 'center' }}
                    title="Delete note">
                    <X size={10} />
                  </button>
                </div>
              ))}
            </div>
          )}

          <div style={{ display: 'flex', gap: 6 }}>
            <input
              value={noteInput}
              onChange={e => setNoteInput(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && addNote()}
              placeholder="Add a note…"
              style={{ flex: 1, background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, padding: '7px 10px', color: 'var(--text1)', fontSize: 12, outline: 'none' }}
            />
            <button onClick={addNote} className={styles.iconBtn} title="Add note" disabled={!noteInput.trim()}>
              <Send size={13} />
            </button>
          </div>
        </div>
      )}
    </div>
  )
}