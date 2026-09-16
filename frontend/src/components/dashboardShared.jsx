// src/components/dashboardShared.jsx
// Shared constants, DB adapter, formatters, and small reusable
// sub-components used across all Dashboard pages.
import { useState, useRef } from 'react'
import { supabase } from '../supabaseClient'
import styles from './Dashboard.module.css'

export const GOOGLE_FORM_URL = 'https://docs.google.com/forms/d/e/1FAIpQLSdSOD2Wrt-Nfk-6YrzUjIO9HyCRRCFzHz8a5uku43Z4fhhaHA/viewform?usp=dialog'

// call_handler.py's base URL — holds the Plivo credentials, so the
// dashboard talks to it for anything Plivo (numbers list, linking a
// number to an agent) rather than calling Plivo's API directly.
export const CALL_HANDLER_URL = import.meta.env.VITE_CALL_HANDLER_URL || 'http://localhost:8000'

// ── DB adapter ────────────────────────────────────────────────
export function normalizeRow(row) {
  const ex = row.extracted || {}
  return {
    ...row,
    lead_category: row.lead_category || 'COLD',
    timestamp: row.created_at,
    summary: ex.summary ?? null,
    next_action: ex.next_action ?? null,
    name: ex.name ?? null,
    pain_points: Array.isArray(ex.pain_points) ? ex.pain_points : [],
    interested_services: Array.isArray(ex.interested_services) ? ex.interested_services : [],
    budget: ex.budget ?? null,
    timeline: ex.timeline ?? null,
    decision_makers: ex.decision_makers ?? null,
  }
}

export function parseTranscript(raw) {
  if (!raw) return []
  if (Array.isArray(raw)) return raw
  if (typeof raw === 'string' && raw.trim()) {
    try { return JSON.parse(raw) } catch { return [{ role: 'Agent', text: raw }] }
  }
  return []
}

export async function fetchLeadsFromSupabase() {
  const { data, error } = await supabase
    .from('calls')
    .select('*')
    .order('created_at', { ascending: false })
    .limit(200)
  if (error) throw new Error(error.message)
  return (data || []).map(normalizeRow)
}

export async function fetchTranscriptFromSupabase(callSid) {
  const { data, error } = await supabase
    .from('calls')
    .select('call_sid, transcript, extracted, to_number, lead_category, created_at, duration_sec')
    .eq('call_sid', callSid)
    .single()
  if (error) throw new Error(error.message)
  const norm = normalizeRow(data)
  return { ...norm, transcript: parseTranscript(data.transcript) }
}

export function computeStats(records) {
  const total = records.length
  const hot = records.filter(r => r.lead_category === 'HOT').length
  const warm = records.filter(r => r.lead_category === 'WARM').length
  const cold = records.filter(r => r.lead_category === 'COLD').length
  const avg = total
    ? (records.reduce((s, r) => s + (r.lead_score || 0), 0) / total).toFixed(1)
    : '0'
  return {
    total_calls: total, hot, warm, cold,
    avg_lead_score: avg,
    conversion_rate: total ? Math.round(hot / total * 100) : 0,
  }
}

// ── constants ─────────────────────────────────────────────────
export const CATEGORY_COLOR = { HOT: '#ff6b4a', WARM: '#f5a623', COLD: '#5b9cf6', CLOSED: '#4ade80' }
export const SCORE_COLOR = s => s >= 8 ? '#ff6b4a' : s >= 5 ? '#f5a623' : '#5b9cf6'
export const PIE_COLORS = ['#ff6b4a', '#f5a623', '#5b9cf6']
export const SOURCE_COLORS = ['#6c63ff', '#4ade80', '#f5a623', '#5b9cf6', '#ff6b4a']
export const ALL_CATS = ['ALL', 'HOT', 'WARM', 'COLD', 'CLOSED']

// ── helpers ───────────────────────────────────────────────────
export function fmtDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })
}

export function fmtDuration(sec) {
  if (sec == null) return '—';
  const minutes = Math.floor(sec / 60);
  const seconds = (sec % 60).toFixed(2);
  return `${minutes}m ${seconds}s`;
}
export function fmtTime(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })
}
export function fmtDateTime(iso) {
  if (!iso) return '—'
  return `${fmtDate(iso)} ${fmtTime(iso)}`
}

// ── toast ─────────────────────────────────────────────────────
export function useToast() {
  const [toast, setToast] = useState(null)
  const t = useRef(null)
  function show(msg, type = 'ok') {
    clearTimeout(t.current)
    setToast({ msg, type })
    t.current = setTimeout(() => setToast(null), 2500)
  }
  return { toast, show }
}
export function Toast({ toast }) {
  if (!toast) return null
  const ok = toast.type === 'ok'
  return (
    <div style={{
      position: 'fixed', bottom: 24, right: 24, zIndex: 999,
      background: ok ? 'rgba(74,222,128,0.12)' : 'rgba(255,107,74,0.12)',
      border: `0.5px solid ${ok ? 'rgba(74,222,128,0.35)' : 'rgba(255,107,74,0.35)'}`,
      color: ok ? 'var(--green)' : 'var(--hot)',
      borderRadius: 10, padding: '10px 18px', fontSize: 13, fontWeight: 500,
      boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
    }}>{toast.msg}</div>
  )
}

// ── export CSV ────────────────────────────────────────────────
export function exportCSV(records, columns, filename) {
  const rows = records.map(r => columns.map(c => `"${String(r[c] ?? '').replace(/"/g, '""')}"`).join(','))
  const blob = new Blob([[columns.join(','), ...rows].join('\n')], { type: 'text/csv' })
  const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: `${filename}_${new Date().toISOString().slice(0, 10)}.csv` })
  a.click(); URL.revokeObjectURL(a.href)
}

// ── Empty State ───────────────────────────────────────────────
export function VisualEmptyState({ message }) {
  return (
    <div style={{ padding: '3rem 1rem', textAlign: 'center', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12 }}>
      <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="var(--text3)" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.6 }}>
        <circle cx="12" cy="12" r="10" />
        <path d="M16 16s-1.5-2-4-2-4 2-4 2" />
        <line x1="9" y1="9" x2="9.01" y2="9" />
        <line x1="15" y1="9" x2="15.01" y2="9" />
      </svg>
      <p style={{ color: 'var(--text2)', fontSize: 13, margin: 0 }}>{message}</p>
    </div>
  )
}

// ── Shared Sub-Components ─────────────────────────────────────
export function StarScore({ score }) {
  const filled = Math.round((score / 10) * 5)
  return (
    <span style={{ color: SCORE_COLOR(score), letterSpacing: 1, fontSize: 13 }}>
      {'★'.repeat(filled)}{'☆'.repeat(5 - filled)}
      <span style={{ color: 'var(--text2)', marginLeft: 5, fontSize: 12 }}>{score}/10</span>
    </span>
  )
}

export function Badge({ category }) {
  const map = {
    HOT: { bg: 'var(--hot-bg)', color: 'var(--hot)' },
    WARM: { bg: 'var(--warm-bg)', color: 'var(--warm)' },
    COLD: { bg: 'var(--cold-bg)', color: 'var(--cold)' },
    CLOSED: { bg: 'rgba(74,222,128,0.15)', color: '#4ade80' }
  }
  const c = map[category] || map.COLD
  return (
    <span style={{ background: c.bg, color: c.color, fontSize: 10, fontWeight: 600, padding: '3px 9px', borderRadius: 20, letterSpacing: 0.5 }}>
      {category || 'COLD'}
    </span>
  )
}

export function MetricCard({ icon: Icon, label, value, sub, color }) {
  return (
    <div className={styles.metricCard}>
      <div className={styles.metricIcon} style={{ color: color || 'var(--accent)' }}><Icon size={18} /></div>
      <div>
        <p className={styles.metricLabel}>{label}</p>
        <p className={styles.metricValue} style={{ color: color || 'var(--text1)' }}>{value}</p>
        {sub && <p className={styles.metricSub}>{sub}</p>}
      </div>
    </div>
  )
}

export function CustomTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div style={{ background: 'var(--bg3)', border: '0.5px solid var(--border2)', borderRadius: 8, padding: '8px 14px', fontSize: 12, color: 'var(--text1)' }}>
      <p style={{ color: 'var(--text2)', marginBottom: 4 }}>{label}</p>
      {payload.map((p, i) => <p key={i} style={{ color: p.color }}>{p.name}: <b>{p.value}</b></p>)}
    </div>
  )
}

export function FilterBar({ value, onChange, cats = ['ALL', 'HOT', 'WARM', 'COLD', 'CLOSED'] }) {
  return (
    <div className={styles.filters}>
      {cats.map(f => (
        <button key={f} onClick={() => onChange(f)}
          className={`${styles.filterBtn} ${value === f ? styles.filterActive : ''}`}
          style={value === f && f !== 'ALL' ? { color: CATEGORY_COLOR[f] } : {}}>
          {f}
        </button>
      ))}
    </div>
  )
}

export function buildWeeklyData(records) {
  const map = {}
  records.forEach(r => {
    const d = r.timestamp ? new Date(r.timestamp) : new Date()
    const key = d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })
    if (!map[key]) map[key] = { date: key, calls: 0, hot: 0, warm: 0, cold: 0 }
    map[key].calls++
    const cat = (r.lead_category || 'COLD').toUpperCase()
    if (cat === 'HOT') map[key].hot++
    if (cat === 'WARM') map[key].warm++
    if (cat === 'COLD') map[key].cold++
  })
  return Object.values(map).slice(-10)
}

export function buildSourceData(records) {
  const map = {}
  records.forEach(r => {
    const s = r.source || 'Unknown'
    map[s] = (map[s] || 0) + 1
  })
  return Object.entries(map).map(([name, value]) => ({ name, value }))
}
