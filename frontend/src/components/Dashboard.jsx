// src/components/Dashboard.jsx
// Root shell — nav, global state (records/stats/search), realtime
// subscription, transcript modal. Each nav page is its own top-level
// file imported below.
import { useEffect, useState, useCallback } from 'react'
import PageSettings from './PageSettings'
import PageDashboard from './PageDashboard'
import PageLeads from './PageLeads'
import PageConversations from './PageConversations'
import PageForms from './PageForms'
import PageAnalytics from './PageAnalytics'
import PageAgentProfiles from './PageAgentProfiles'
import {
  RefreshCw, Search, AlertCircle, Radio, X,
  Activity, Users, FileText, BarChart2, MessageSquare, ClipboardList, Settings,
} from 'lucide-react'
import { supabase } from '../supabaseClient'
import styles from './Dashboard.module.css'
import {
  fetchLeadsFromSupabase, fetchTranscriptFromSupabase, computeStats,
  useToast, Toast, VisualEmptyState,
} from './dashboardShared'

const NAV = [
  { id: 'dashboard', icon: Activity, label: 'Dashboard' },
  { id: 'leads', icon: Users, label: 'Leads' },
  { id: 'conversations', icon: FileText, label: 'Conversations' },
  { id: 'analytics', icon: BarChart2, label: 'Analytics' },
  { id: 'prompt', icon: MessageSquare, label: 'Agent Profiles & Calling' },
  { id: 'forms', icon: ClipboardList, label: 'Forms', badgeKey: 'forms' },
  { id: 'settings', icon: Settings, label: 'Settings' },
]

const PAGE_STORAGE_KEY = 'dashboard_active_page'
const NAV_IDS = NAV.map(n => n.id)

function loadInitialPage() {
  try {
    const saved = localStorage.getItem(PAGE_STORAGE_KEY)
    return NAV_IDS.includes(saved) ? saved : 'dashboard'
  } catch {
    return 'dashboard'
  }
}

export default function Dashboard() {
  const [page, setPageState] = useState(loadInitialPage)
  const setPage = useCallback((id) => {
    setPageState(id)
    try { localStorage.setItem(PAGE_STORAGE_KEY, id) } catch {}
  }, [])
  const [records, setRecords] = useState([])
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [filter, setFilter] = useState('ALL')
  const [selected, setSelected] = useState(null)
  const [lastSync, setLastSync] = useState(null)
  const [agentConfig, setAgentConfig] = useState({})
  const [globalSearch, setGlobalSearch] = useState('')
  const [liveCall, setLiveCall] = useState(null)
  const [formCount, setFormCount] = useState(0)

  const { toast, show: showToast } = useToast()

  const fetchAll = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const leads = await fetchLeadsFromSupabase()
      setRecords(leads)
      setStats(computeStats(leads))
      setLastSync(new Date())
    } catch (e) {
      console.error('[Dashboard] fetch error:', e)
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    supabase.from('agent_config').select('key, value').then(({ data, error }) => {
      if (error) console.error('[agentConfig load]', error.message)
      const map = {}
        ; (data || []).forEach(r => { map[r.key] = r.value })
      setAgentConfig(map)
    })
  }, [])

  useEffect(() => {
    fetchAll()
    const channel = supabase
      .channel('calls-realtime-global')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'calls' }, (payload) => {
        if (payload.eventType === 'INSERT') {
          setLiveCall({ sid: payload.new.call_sid, from: payload.new.from_number, status: 'Connected' })
          setTimeout(() => setLiveCall(null), 8000)
        }
        fetchAll()
      })
      .subscribe()
    return () => supabase.removeChannel(channel)
  }, [fetchAll])

  async function openTranscript(callSid) {
    try {
      const data = await fetchTranscriptFromSupabase(callSid)
      setSelected(data)
    } catch (e) {
      setSelected({ call_sid: callSid, transcript: [], error: e.message })
    }
  }

  const pageTitle = NAV.find(n => n.id === page)?.label || 'Dashboard'

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.logo}>
          <Activity size={20} color="var(--accent)" />
          <span>Inbox Infotech</span>
        </div>
        <nav className={styles.nav}>
          {NAV.map(({ id, icon: Icon, label, badgeKey }) => (
            <button key={id} onClick={() => setPage(id)}
              className={`${styles.navItem} ${page === id ? styles.navActive : ''}`}>
              <Icon size={16} />
              <span style={{ flex: 1, textAlign: 'left' }}>{label}</span>
              {badgeKey === 'forms' && formCount > 0 && (
                <span style={{ background: 'var(--accent)', color: '#fff', fontSize: 10, padding: '2px 6px', borderRadius: 10, fontWeight: 600 }}>
                  {formCount}
                </span>
              )}
            </button>
          ))}
        </nav>
      </aside>

      <main className={styles.main}>
        {liveCall && (
          <div style={{ background: 'rgba(255,107,74,0.15)', border: '1px solid var(--hot)', padding: '10px 16px', borderRadius: 10, marginBottom: 14, display: 'flex', alignItems: 'center', gap: 10, color: 'var(--hot)' }}>
            <Radio size={16} className={styles.spin} />
            <span style={{ fontSize: 13, fontWeight: 500 }}>
              <b>Live Call:</b> Incoming from {liveCall.from || 'anonymous'} ({liveCall.sid?.slice(0, 8)}…)
            </span>
            <button onClick={() => setLiveCall(null)} style={{ marginLeft: 'auto', background: 'transparent', border: 'none', color: 'var(--hot)', cursor: 'pointer' }}>
              <X size={14} />
            </button>
          </div>
        )}

        <div className={styles.header}>
          <div>
            <h1 className={styles.pageTitle}>{pageTitle}</h1>
            <p className={styles.pageSub}>
              {lastSync ? `Last synced ${lastSync.toLocaleTimeString()}` : loading ? 'Fetching…' : 'Not yet synced'}
            </p>
          </div>

          {/* Search only makes sense where records are actually filtered by it — Leads and Conversations. */}
          {['leads', 'conversations'].includes(page) && (
            <div style={{ position: 'relative', width: 320 }}>
              <Search size={14} style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text3)' }} />
              <input
                value={globalSearch}
                onChange={e => setGlobalSearch(e.target.value)}
                placeholder="Search by name, phone, summary…"
                style={{ width: '100%', background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 8, padding: '7px 12px 7px 32px', color: 'var(--text1)', fontSize: 12, outline: 'none', boxSizing: 'border-box' }}
              />
              {globalSearch && (
                <X size={12} onClick={() => setGlobalSearch('')}
                  style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', color: 'var(--text3)', cursor: 'pointer' }} />
              )}
            </div>
          )}

          {!['prompt', 'settings'].includes(page) && (
            <button className={styles.refreshBtn} onClick={fetchAll} disabled={loading}>
              <RefreshCw size={14} className={loading ? styles.spin : ''} />
              Refresh
            </button>
          )}
        </div>

        {error && (
          <div className={styles.errorBanner}>
            <AlertCircle size={14} />
            <span><b>Supabase error:</b> {error}</span>
          </div>
        )}

        {page === 'dashboard' && <PageDashboard records={records} stats={stats} loading={loading} filter={filter} setFilter={setFilter} openTranscript={openTranscript} showToast={showToast} />}
        {page === 'leads' && <PageLeads records={records} loading={loading} openTranscript={openTranscript} showToast={showToast} fetchAll={fetchAll} agentConfig={agentConfig} globalSearch={globalSearch} goToAgentProfiles={() => setPage('prompt')} />}
        {page === 'conversations' && <PageConversations records={records} loading={loading} openTranscript={openTranscript} globalSearch={globalSearch} />}
        {page === 'forms' && <PageForms showToast={showToast} setFormCount={setFormCount} />}
        {page === 'analytics' && <PageAnalytics records={records} stats={stats} loading={loading} />}
        {page === 'prompt' && <PageAgentProfiles showToast={showToast} />}
        {page === 'settings' && <PageSettings supabase={supabase} showToast={showToast} onConfigChange={cfg => setAgentConfig(c => ({ ...c, ...cfg }))} />}
      </main>

      {selected && (
        <div className={styles.modalOverlay} onClick={() => setSelected(null)}>
          <div className={styles.modal} onClick={e => e.stopPropagation()}>
            <div className={styles.modalHeader}>
              <h3>Transcript — <span className={styles.mono}>{selected.call_sid}</span></h3>
              <button className={styles.iconBtn} onClick={() => setSelected(null)}><X size={14} /></button>
            </div>
            {selected.error && (
              <div style={{ padding: '12px 1.25rem', color: 'var(--hot)', fontSize: 13 }}>Error: {selected.error}</div>
            )}
            <div className={styles.transcriptBody}>
              {selected.transcript?.length === 0 && !selected.error && (
                <VisualEmptyState message="No transcript data for this call" />
              )}
              {(selected.transcript || []).map((line, i) => {
                const isAgent = line.role === 'Agent'
                return (
                  <div key={i} className={`${styles.bubble} ${isAgent ? styles.bubbleAgent : styles.bubbleCustomer}`}>
                    <span className={styles.bubbleLabel}>{line.role}</span>
                    <p>{line.text}</p>
                  </div>
                )
              })}
            </div>
          </div>
        </div>
      )}

      <Toast toast={toast} />
    </div>
  )
}