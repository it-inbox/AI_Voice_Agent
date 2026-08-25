// src/components/PageDashboard.jsx
import { useEffect, useState, useMemo } from 'react'
import {
  AreaChart, Area, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { Users, Flame, Phone, TrendingUp, CheckCircle2, Download } from 'lucide-react'
import styles from './Dashboard.module.css'
import {
  CATEGORY_COLOR, PIE_COLORS, SOURCE_COLORS,
  fmtDate, fmtDuration, fmtDateTime,
  VisualEmptyState, StarScore, Badge, MetricCard, CustomTooltip, FilterBar,
  buildWeeklyData, buildSourceData,
} from './dashboardShared'

export default function PageDashboard({ records, stats, loading, filter, setFilter, openTranscript, showToast, globalSearch }) {
  const total = stats?.total_calls ?? records.length
  const hot = stats?.hot ?? records.filter(r => r.lead_category === 'HOT').length
  const warm = stats?.warm ?? records.filter(r => r.lead_category === 'WARM').length
  const cold = stats?.cold ?? records.filter(r => r.lead_category === 'COLD').length
  const avgScore = stats?.avg_lead_score ?? (records.length ? (records.reduce((s, r) => s + (r.lead_score || 0), 0) / records.length).toFixed(1) : '0')
  const convRate = stats?.conversion_rate ?? (total ? Math.round(hot / total * 100) : 0)

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)

  const filteredRecords = useMemo(() => {
    return records
      .filter(r => filter === 'ALL' || r.lead_category === filter)
      .filter(r => !globalSearch ||
        (r.to_number || '').includes(globalSearch) ||
        (r.name || '').toLowerCase().includes(globalSearch.toLowerCase()) ||
        (r.summary || '').toLowerCase().includes(globalSearch.toLowerCase()))
  }, [records, filter, globalSearch])

  useEffect(() => {
    setPage(1)
  }, [filter, globalSearch, pageSize])

  const totalPages = Math.max(1, Math.ceil(filteredRecords.length / pageSize))
  const pagedRecords = useMemo(() => {
    const start = (page - 1) * pageSize
    return filteredRecords.slice(start, start + pageSize)
  }, [filteredRecords, page, pageSize])

  const exportCsv = () => {
    if (!filteredRecords.length) {
      showToast?.('No records to export')
      return
    }
    const headers = ['Phone', 'Lead Status', 'Score', 'Duration', 'Summary', 'Date', 'Last Contacted', 'Call Status']
    const rows = filteredRecords.map(r => [
      r.to_number || '',
      r.lead_category || '',
      r.lead_score || '',
      fmtDuration(r.duration_sec),
      (r.summary || '').replace(/"/g, '""'),
      fmtDate(r.timestamp),
      r.last_contacted_at ? fmtDateTime(r.last_contacted_at) : fmtDateTime(r.timestamp),
      r.live_outcome || '',
    ])
    const csv = [headers, ...rows]
      .map(row => row.map(cell => `"${String(cell)}"`).join(','))
      .join('\n')
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `leads_export_${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    showToast?.('CSV exported')
  }

  const weeklyData = buildWeeklyData(records)
  const pieData = [
    { name: 'Hot', value: hot, pct: total ? Math.round(hot / total * 100) : 0 },
    { name: 'Warm', value: warm, pct: total ? Math.round(warm / total * 100) : 0 },
    { name: 'Cold', value: cold, pct: total ? Math.round(cold / total * 100) : 0 },
  ]
  const sourceData = buildSourceData(records)

  return (
    <>
      <div className={styles.metricsRow}>
        <MetricCard icon={Users} label="Total leads" value={loading ? '…' : total} sub="all time" />
        <MetricCard icon={Flame} label="Hot leads" value={loading ? '…' : hot} sub={`${Math.round(hot / Math.max(total, 1) * 100)}% of total`} color="var(--hot)" />
        <MetricCard icon={Phone} label="Total calls" value={loading ? '…' : total} sub="processed" color="var(--accent)" />
        <MetricCard icon={TrendingUp} label="Conversion" value={loading ? '…' : `${convRate}%`} sub="hot / total" color="var(--green)" />
        <MetricCard icon={CheckCircle2} label="Avg score" value={loading ? '…' : avgScore} sub="out of 10" color="var(--warm)" />
      </div>

      <div className={styles.chartsRow}>
        <div className={styles.chartCard}>
          <h3 className={styles.chartTitle}>Calls over time</h3>
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={weeklyData} margin={{ top: 5, right: 10, bottom: 0, left: -20 }}>
              <defs>
                <linearGradient id="gHot" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#ff6b4a" stopOpacity={0.3} />
                  <stop offset="100%" stopColor="#ff6b4a" stopOpacity={0} />
                </linearGradient>
                <linearGradient id="gWarm" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#f5a623" stopOpacity={0.3} />
                  <stop offset="100%" stopColor="#f5a623" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
              <XAxis dataKey="date" tick={{ fill: 'var(--text2)', fontSize: 11 }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fill: 'var(--text2)', fontSize: 11 }} axisLine={false} tickLine={false} />
              <Tooltip content={<CustomTooltip />} />
              <Area type="monotone" dataKey="hot" name="Hot" stroke="#ff6b4a" fill="url(#gHot)" strokeWidth={2} dot={false} />
              <Area type="monotone" dataKey="warm" name="Warm" stroke="#f5a623" fill="url(#gWarm)" strokeWidth={2} dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        <div className={styles.chartCard}>
          <h3 className={styles.chartTitle}>Lead breakdown</h3>
          <ResponsiveContainer width="100%" height={160}>
            <PieChart>
              <Pie data={pieData} cx="50%" cy="50%" innerRadius={45} outerRadius={70} dataKey="value" paddingAngle={3}>
                {pieData.map((_, i) => <Cell key={i} fill={PIE_COLORS[i]} />)}
              </Pie>
              <Tooltip content={<CustomTooltip />} />
            </PieChart>
          </ResponsiveContainer>
          <div className={styles.pieLegend}>
            {pieData.map((d, i) => (
              <span key={i} className={styles.legItem}>
                <span className={styles.legDot} style={{ background: PIE_COLORS[i] }} />
                {d.name} {d.pct}%
              </span>
            ))}
          </div>
        </div>

        <div className={styles.chartCard}>
          <h3 className={styles.chartTitle}>Top sources</h3>
          {sourceData.length === 0 ? (
            <VisualEmptyState message="No source data found" />
          ) : (
            <>
              <ResponsiveContainer width="100%" height={160}>
                <PieChart>
                  <Pie data={sourceData} cx="50%" cy="50%" innerRadius={45} outerRadius={70} dataKey="value" paddingAngle={3}>
                    {sourceData.map((_, i) => <Cell key={i} fill={SOURCE_COLORS[i % SOURCE_COLORS.length]} />)}
                  </Pie>
                  <Tooltip content={<CustomTooltip />} />
                </PieChart>
              </ResponsiveContainer>
              <div className={styles.pieLegend}>
                {sourceData.map((d, i) => (
                  <span key={i} className={styles.legItem}>
                    <span className={styles.legDot} style={{ background: SOURCE_COLORS[i % SOURCE_COLORS.length] }} />
                    {d.name}
                  </span>
                ))}
              </div>
            </>
          )}
        </div>
      </div>

      <div className={styles.tableCard}>
        <div className={styles.tableHeader}>
          <h3 className={styles.chartTitle} style={{ margin: 0 }}>Recent leads</h3>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <FilterBar value={filter} onChange={setFilter} cats={['ALL', 'HOT', 'WARM', 'COLD', 'CLOSED']} />
            <select
              value={pageSize}
              onChange={e => setPageSize(Number(e.target.value))}
              style={{
                background: 'var(--bg2, #1a1a24)',
                color: 'var(--text2)',
                border: '1px solid rgba(255,255,255,0.1)',
                borderRadius: 8,
                padding: '6px 10px',
                fontSize: 12,
                cursor: 'pointer',
              }}
            >
              <option value={10}>10 per page</option>
              <option value={25}>25 per page</option>
              <option value={50}>50 per page</option>
              <option value={100}>100 per page</option>
            </select>
            <button
              onClick={exportCsv}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                background: 'var(--bg2, #1a1a24)',
                color: 'var(--text2)',
                border: '1px solid rgba(255,255,255,0.1)',
                borderRadius: 8,
                padding: '6px 12px',
                fontSize: 12,
                cursor: 'pointer',
              }}
            >
              <Download size={14} />
              Export CSV
            </button>
          </div>
        </div>
        <div className={styles.tableWrap}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Phone</th>
                <th>Lead Status</th>
                <th>Score</th>
                <th>Duration</th>
                <th>Summary</th>
                <th>Date</th>
                <th>Last Contacted</th>
                <th>Call Status</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={8} className={styles.emptyRow}>Loading…</td></tr>
              ) : pagedRecords.length === 0 ? (
                <tr><td colSpan={8}><VisualEmptyState message="No matching recent records found" /></td></tr>
              ) : pagedRecords.map(r => (
                <tr key={r.call_sid} className={styles.tableRow}>
                  <td className={styles.mono}>{r.to_number || '—'}</td>
                  <td><Badge category={r.lead_category} /></td>
                  <td><StarScore score={r.lead_score || 1} /></td>
                  <td style={{ color: 'var(--text2)' }}>{fmtDuration(r.duration_sec)}</td>
                  <td className={styles.summaryCell}>{r.summary || '—'}</td>
                  <td style={{ color: 'var(--text2)', whiteSpace: 'nowrap' }}>{fmtDate(r.timestamp)}</td>
                  <td style={{ color: 'var(--text2)', fontSize: 12 }}>{r.last_contacted_at ? fmtDateTime(r.last_contacted_at) : fmtDateTime(r.timestamp)}</td>
                  <td className={styles.mono} >{r.live_outcome || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
          <p className={styles.tableFooter} style={{ margin: 0 }}>
            Showing {pagedRecords.length ? (page - 1) * pageSize + 1 : 0}–{Math.min(page * pageSize, filteredRecords.length)} of {filteredRecords.length} records
          </p>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page <= 1}
              style={{
                background: 'transparent', color: 'var(--text2)',
                border: '1px solid rgba(255,255,255,0.1)', borderRadius: 6,
                padding: '4px 10px', fontSize: 12,
                cursor: page <= 1 ? 'not-allowed' : 'pointer',
                opacity: page <= 1 ? 0.4 : 1,
              }}
            >
              Prev
            </button>
            <span style={{ color: 'var(--text2)', fontSize: 12 }}>Page {page} of {totalPages}</span>
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              style={{
                background: 'transparent', color: 'var(--text2)',
                border: '1px solid rgba(255,255,255,0.1)', borderRadius: 6,
                padding: '4px 10px', fontSize: 12,
                cursor: page >= totalPages ? 'not-allowed' : 'pointer',
                opacity: page >= totalPages ? 0.4 : 1,
              }}
            >
              Next
            </button>
          </div>
        </div>
      </div>
    </>
  )
}
