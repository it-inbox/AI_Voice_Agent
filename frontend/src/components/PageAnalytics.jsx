// src/components/PageAnalytics.jsx
import { useState } from 'react'
import {
  BarChart, Bar, AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell,
} from 'recharts'
import { TrendingUp, CheckCircle2, Flame, Phone, Users } from 'lucide-react'
import styles from './Dashboard.module.css'
import { CATEGORY_COLOR, VisualEmptyState, MetricCard, CustomTooltip, FilterBar } from './dashboardShared'

// ── period options + bucketing (local to Analytics page) ──────
const ANALYTICS_PERIODS = [
  { id: 'weekly',  label: 'Weekly'  },
  { id: 'monthly', label: 'Monthly' },
  { id: 'yearly',  label: 'Yearly'  },
]


// ══════════════════════════════════════════════════════════════
// Keyword mention mining — used for "Top interested services" chart.
// Reads real text per call: extracted.summary (jsonb, set by lead
// analysis step) → falls back to transcript if summary missing.
// This REPLACES the old `interested_services` array approach, which
// fragmented identical services into near-duplicate bars (e.g.
// "AI/ML Development" vs "AI / ML Development" vs "Mobile & Web
// Development" vs "Mobile and Web Development" — all separate bars
// for the same thing). Keyword groups normalize those variants into
// one bucket, and are mined straight from what the customer actually
// said, not from a possibly-inconsistent tag written elsewhere.
//
// EDIT THIS LIST to match what your agent actually pitches — these
// are generic placeholders. Each entry: label shown in the chart,
// and one or more match patterns (case-insensitive, substring).
// ══════════════════════════════════════════════════════════════
const TECH_KEYWORDS = [
  { label: 'CRM',                 patterns: ['crm', 'lead management', 'lead tracking'] },
  { label: 'WhatsApp',            patterns: ['whatsapp'] },
  { label: 'AI / ML Development', patterns: ['ai/ml', 'ai / ml', 'machine learning', 'ai agent', 'artificial intelligence', 'chatbot', 'voice bot', 'voice agent', 'automation'] },
  { label: 'Web Development',     patterns: ['website', 'web development', 'landing page'] },
  { label: 'Mobile Development',  patterns: ['mobile app', 'mobile development', 'android app', 'ios app'] },
  { label: 'Payments',            patterns: ['payment gateway', 'upi', 'online payment'] },
  { label: 'Cloud / Server',      patterns: ['cloud', 'server', 'hosting'] },
  { label: 'Integration/API',     patterns: ['integration', 'api', 'zapier', 'sync with'] },
  { label: 'Analytics/Dashboard', patterns: ['dashboard', 'analytics', 'reporting'] },
]

// counts, per record, how many DISTINCT keyword groups appear in the
// text — a record mentioning "crm" three times still counts once
// per group, so one gushing call doesn't skew the whole chart
function buildKeywordMentions(records, keywordGroups) {
  const counts = {}
  keywordGroups.forEach(g => { counts[g.label] = 0 })

  records.forEach(r => {
    // real schema has no `summary` column — text lives in extracted.summary
    // (jsonb, set by lead analysis step). Fall back to transcript if absent.
    const text = (
      r.extracted?.summary ||
      r.summary || // kept for safety if a `summary` column gets added later
      r.transcript ||
      ''
    ).toLowerCase()
    if (!text) return
    keywordGroups.forEach(g => {
      const hit = g.patterns.some(p => text.includes(p.toLowerCase()))
      if (hit) counts[g.label]++
    })
  })

  return Object.entries(counts)
    .map(([name, count]) => ({ name, count }))
    .filter(d => d.count > 0)
    .sort((a, b) => b.count - a.count)
    .slice(0, 8)
}

function getAnalyticsPeriodConfig(period) {
  const now = new Date()
  if (period === 'weekly') {
    const cutoff = new Date(now); cutoff.setDate(cutoff.getDate() - 7)
    return { cutoff }
  }
  if (period === 'yearly') {
    const cutoff = new Date(now); cutoff.setFullYear(cutoff.getFullYear() - 1)
    return { cutoff }
  }
  const cutoff = new Date(now); cutoff.setDate(cutoff.getDate() - 30)
  return { cutoff }
}

function filterRecordsByPeriod(records, period) {
  const { cutoff } = getAnalyticsPeriodConfig(period)
  return records.filter(r => r.timestamp && new Date(r.timestamp) >= cutoff)
}

// ══════════════════════════════════════════════════════════════
// Real calendar-date buckets — BUG FIX: the old version bucketed by
// weekday name (Sun..Sat) or bare month name (Jan..Dec) with a fixed
// order. That silently merges different calendar weeks/years into the
// same bar — e.g. last Monday and this Monday landed in the same "Mon"
// bucket, so the chart looked "stuck" and just swapped one bar's value
// week to week instead of showing a real rolling week. Same flaw hit
// the yearly view across a year boundary (Aug '25 and Aug '26 both
// just "Aug"). Buckets are now built from actual dates going backward
// from today, each with a unique key, so every real day/month gets its
// own bar and nothing collides across period boundaries.
// ══════════════════════════════════════════════════════════════
function buildDateBuckets(period) {
  const now = new Date()
  const buckets = []
  if (period === 'weekly') {
    for (let i = 6; i >= 0; i--) {
      const d = new Date(now); d.setDate(d.getDate() - i)
      buckets.push({
        key: d.toDateString(),
        label: `${d.toLocaleDateString('en-IN', { weekday: 'short' })} ${d.getDate()}`,
      })
    }
  } else if (period === 'yearly') {
    for (let i = 11; i >= 0; i--) {
      const d = new Date(now.getFullYear(), now.getMonth() - i, 1)
      buckets.push({
        key: `${d.getFullYear()}-${d.getMonth()}`,
        label: d.toLocaleDateString('en-IN', { month: 'short', year: '2-digit' }),
      })
    }
  } else {
    for (let i = 29; i >= 0; i--) {
      const d = new Date(now); d.setDate(d.getDate() - i)
      buckets.push({
        key: d.toDateString(),
        label: d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }),
      })
    }
  }
  return buckets
}

function bucketKeyForDate(date, period) {
  if (period === 'yearly') return `${date.getFullYear()}-${date.getMonth()}`
  return date.toDateString()
}

function buildScoreSeries(records, period) {
  const buckets = buildDateBuckets(period)
  const sums = {}
  records.forEach(r => {
    if (!r.timestamp) return
    const key = bucketKeyForDate(new Date(r.timestamp), period)
    if (!sums[key]) sums[key] = { scoreSum: 0, scoreN: 0 }
    sums[key].scoreSum += (r.lead_score || 0)
    sums[key].scoreN++
  })
  return buckets.map(b => {
    const s = sums[b.key]
    return { date: b.label, avg_score: s && s.scoreN ? +(s.scoreSum / s.scoreN).toFixed(1) : 0 }
  })
}

function buildCallsSeries(records, period) {
  const buckets = buildDateBuckets(period)
  const counts = {}
  records.forEach(r => {
    if (!r.timestamp) return
    const key = bucketKeyForDate(new Date(r.timestamp), period)
    counts[key] = (counts[key] || 0) + 1
  })
  return buckets.map(b => ({ day: b.label, calls: counts[b.key] || 0 }))
}

// ── dropdown — reuses existing .filters / .filterBtn / .filterActive ──
function AnalyticsPeriodDropdown({ value, onChange }) {
  return (
    <div className={styles.filters}>
      {ANALYTICS_PERIODS.map(p => (
        <button
          key={p.id}
          onClick={() => onChange(p.id)}
          className={`${styles.filterBtn} ${value === p.id ? styles.filterActive : ''}`}
        >
          {p.label}
        </button>
      ))}
    </div>
  )
}

export default function PageAnalytics({ records, stats, loading }) {
  const [period, setPeriod] = useState('monthly')

  // filter to selected window, derive everything below from this subset
  const periodRecords = filterRecordsByPeriod(records, period)

  const total    = periodRecords.length
  const hot      = periodRecords.filter(r => r.lead_category === 'HOT').length
  const warm     = periodRecords.filter(r => r.lead_category === 'WARM').length
  const cold     = periodRecords.filter(r => r.lead_category === 'COLD').length
  const convRate = total ? Math.round(hot / total * 100) : 0
  const avgScore = periodRecords.length
    ? (periodRecords.reduce((s, r) => s + (r.lead_score || 0), 0) / periodRecords.length).toFixed(1)
    : '0'

  const durationData = ['HOT', 'WARM', 'COLD'].map(cat => {
    const rows = periodRecords.filter(r => r.lead_category === cat && r.duration_sec)
    const avg  = rows.length ? Math.round(rows.reduce((s, r) => s + r.duration_sec, 0) / rows.length) : 0
    return { category: cat, avg_min: +(avg / 60).toFixed(1) }
  })

  const callsData = buildCallsSeries(periodRecords, period)

  const scoreOverTime = buildScoreSeries(periodRecords, period)

  // "Top interested services" — now mined directly from call summaries
  // instead of the raw `interested_services` array, so near-duplicate
  // labels ("AI/ML Development" vs "AI / ML Development" etc.) collapse
  // into one real bucket, straight from what the customer said.
  const svcData = buildKeywordMentions(periodRecords, TECH_KEYWORDS)

  const periodLabel = period === 'weekly' ? 'Last 7 days' : period === 'yearly' ? 'Last 12 months' : 'Last 30 days'

  return (
    <>
      {/* top-right period dropdown */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
        <AnalyticsPeriodDropdown value={period} onChange={setPeriod} />
      </div>

      <div className={styles.metricsRow}>
        <MetricCard icon={TrendingUp}    label="Conversion Rate" value={loading ? '…' : `${convRate}%`} sub={periodLabel} color="var(--green)" />
        <MetricCard icon={CheckCircle2}  label="Avg Lead Score"  value={loading ? '…' : avgScore}        sub={periodLabel} color="var(--warm)" />
        <MetricCard icon={Flame}         label="Hot Leads"       value={loading ? '…' : hot}             sub={`${total} total calls`} color="var(--hot)" />
        <MetricCard icon={Phone}         label="Warm Leads"      value={loading ? '…' : warm}            sub={`${total ? Math.round(warm/total*100) : 0}% of total`} color="var(--warm)" />
        <MetricCard icon={Users}         label="Cold Leads"      value={loading ? '…' : cold}            sub={`${total ? Math.round(cold/total*100) : 0}% of total`} color="var(--cold)" />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: '1.5rem' }}>
        <div className={styles.chartCard}>
          <h3 className={styles.chartTitle}>Avg call duration by category (min) · {periodLabel}</h3>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={durationData} margin={{ top: 5, right: 10, bottom: 0, left: -20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
              <XAxis dataKey="category" tick={{ fill: 'var(--text2)', fontSize: 11 }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: 'var(--text2)', fontSize: 11 }} axisLine={false} tickLine={false} />
              <Tooltip content={<CustomTooltip />} />
              <Bar dataKey="avg_min" name="Avg mins" radius={[4,4,0,0]}>
                {durationData.map((d, i) => <Cell key={i} fill={CATEGORY_COLOR[d.category]} fillOpacity={0.85} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className={styles.chartCard}>
          <h3 className={styles.chartTitle}>
            Calls per {period === 'yearly' ? 'month' : 'day'} · {periodLabel}
          </h3>
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={callsData} margin={{ top: 5, right: 10, bottom: 0, left: -20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
              <XAxis dataKey="day" tick={{ fill: 'var(--text2)', fontSize: 11 }} axisLine={false} tickLine={false} interval={period === 'monthly' ? 2 : 0} />
              <YAxis tick={{ fill: 'var(--text2)', fontSize: 11 }} axisLine={false} tickLine={false} allowDecimals={false} />
              <Tooltip content={<CustomTooltip />} />
              <Bar dataKey="calls" name="Calls" fill="var(--accent)" fillOpacity={0.8} radius={[4,4,0,0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className={styles.chartCard}>
          <h3 className={styles.chartTitle}>
            Avg lead score over time — {period === 'yearly' ? 'by month' : 'by day'} · {periodLabel}
          </h3>
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={scoreOverTime} margin={{ top: 5, right: 10, bottom: 0, left: -20 }}>
              <defs>
                <linearGradient id="gScore" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%"   stopColor="#4ade80" stopOpacity={0.3} />
                  <stop offset="100%" stopColor="#4ade80" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
              <XAxis dataKey="date" tick={{ fill: 'var(--text2)', fontSize: 11 }} axisLine={false} tickLine={false} />
              <YAxis domain={[0,10]} tick={{ fill: 'var(--text2)', fontSize: 11 }} axisLine={false} tickLine={false} />
              <Tooltip content={<CustomTooltip />} />
              <Area type="monotone" dataKey="avg_score" name="Avg score" stroke="#4ade80" fill="url(#gScore)" strokeWidth={2} dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        <div className={styles.chartCard}>
          <h3 className={styles.chartTitle}>Top interested services (from summaries) · {periodLabel}</h3>
          {svcData.length === 0 ? (
            <VisualEmptyState message="No service mentions found in this period's summaries" />
          ) : (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart layout="vertical" data={svcData} margin={{ top: 5, right: 10, bottom: 0, left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                <XAxis type="number" tick={{ fill: 'var(--text2)', fontSize: 11 }} axisLine={false} tickLine={false} allowDecimals={false} />
                <YAxis type="category" dataKey="name" tick={{ fill: 'var(--text2)', fontSize: 10 }} axisLine={false} tickLine={false} width={120} />
                <Tooltip content={<CustomTooltip />} />
                <Bar dataKey="count" name="Mentions" fill="var(--accent)" fillOpacity={0.8} radius={[0,4,4,0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
          <p style={{ fontSize: 10, color: 'var(--text3)', margin: '8px 0 0' }}>
            Mined from call summaries, not the raw interested_services tag — edit TECH_KEYWORDS in code to match what your agent pitches.
          </p>
        </div>
      </div>
    </>
  )
}