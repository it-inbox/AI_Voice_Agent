// src/components/PageConversations.jsx
import { useEffect, useState, useMemo } from 'react'
import styles from './Dashboard.module.css'
import { fetchTranscriptFromSupabase, fmtDate, fmtDuration, Badge, VisualEmptyState } from './dashboardShared'

export default function PageConversations({ records, loading, openTranscript, globalSearch }) {
  const [selected, setSelected] = useState(null)
  const [txData, setTxData] = useState(null)
  const [txLoading, setTxLoading] = useState(false)

  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)

  const filtered = useMemo(() => {
    return records.filter(r =>
      !globalSearch ||
      (r.to_number || '').includes(globalSearch) ||
      (r.name || '').toLowerCase().includes(globalSearch.toLowerCase()) ||
      (r.summary || '').toLowerCase().includes(globalSearch.toLowerCase())
    )
  }, [records, globalSearch])

  useEffect(() => {
    setPage(1)
  }, [globalSearch, pageSize])

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const paged = useMemo(() => {
    const start = (page - 1) * pageSize
    return filtered.slice(start, start + pageSize)
  }, [filtered, page, pageSize])

  async function loadTranscript(r) {
    setSelected(r)
    setTxData(null)
    setTxLoading(true)
    try {
      const data = await fetchTranscriptFromSupabase(r.call_sid)
      console.log('RAW TRANSCRIPT:', JSON.stringify(data?.transcript)) // debug — check for \n chars
      setTxData(data)
    } catch (e) {
      setTxData({ error: e.message, transcript: '' })
    } finally {
      setTxLoading(false)
    }
  }

  return (
    <div style={{ display: 'flex', gap: 16, height: 'calc(100vh - 140px)' }}>
      <div style={{ width: 300, flexShrink: 0, display: 'flex', flexDirection: 'column', gap: 8, overflow: 'hidden' }}>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 2 }}>
          <select
            value={pageSize}
            onChange={e => setPageSize(Number(e.target.value))}
            style={{
              background: 'var(--bg2)',
              color: 'var(--text2)',
              border: '1px solid var(--border)',
              borderRadius: 8,
              padding: '4px 8px',
              fontSize: 11,
              cursor: 'pointer',
            }}
          >
            <option value={10}>10 per page</option>
            <option value={25}>25 per page</option>
            <option value={50}>50 per page</option>
            <option value={100}>100 per page</option>
          </select>
        </div>

        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8, overflowY: 'auto' }}>
          {loading && <p style={{ color: 'var(--text2)', textAlign: 'center', padding: '2rem', fontSize: 13 }}>Loading…</p>}
          {paged.map(r => (
            <div key={r.call_sid}
              onClick={() => loadTranscript(r)}
              style={{
                background: selected?.call_sid === r.call_sid ? 'var(--bg3)' : 'var(--bg2)',
                border: `0.5px solid ${selected?.call_sid === r.call_sid ? 'var(--border2)' : 'var(--border)'}`,
                borderRadius: 10, padding: '10px 12px', cursor: 'pointer',
              }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                <span style={{ fontWeight: 500 }}>{r.name || r.to_number || '—'}</span>
                <Badge category={r.lead_category} />
              </div>
              <p style={{ fontSize: 12, color: 'var(--text2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {r.summary || 'No summary'}
              </p>
              <p style={{ fontSize: 11, color: 'var(--text3)', marginTop: 4 }}>{fmtDate(r.timestamp)} · {fmtDuration(r.duration_sec)}</p>
            </div>
          ))}
          {!loading && filtered.length === 0 && (
            <VisualEmptyState message="No matching conversation records" />
          )}
        </div>

        {!loading && filtered.length > 0 && (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', paddingTop: 4 }}>
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page <= 1}
              style={{
                background: 'transparent', color: 'var(--text2)',
                border: '1px solid var(--border)', borderRadius: 6,
                padding: '4px 10px', fontSize: 11,
                cursor: page <= 1 ? 'not-allowed' : 'pointer',
                opacity: page <= 1 ? 0.4 : 1,
              }}
            >
              Prev
            </button>
            <span style={{ color: 'var(--text2)', fontSize: 11 }}>Page {page} of {totalPages}</span>
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              style={{
                background: 'transparent', color: 'var(--text2)',
                border: '1px solid var(--border)', borderRadius: 6,
                padding: '4px 10px', fontSize: 11,
                cursor: page >= totalPages ? 'not-allowed' : 'pointer',
                opacity: page >= totalPages ? 0.4 : 1,
              }}
            >
              Next
            </button>
          </div>
        )}
      </div>

      <div style={{ flex: 1, background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 14, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        {!selected ? (
          <div style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <VisualEmptyState message="Select a conversation to load transcript" />
          </div>
        ) : (
          <>
            <div style={{ padding: '1rem 1.25rem', borderBottom: '0.5px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <span style={{ fontWeight: 600 }}>{selected.name || selected.to_number || '—'}</span>
                <span style={{ marginLeft: 10 }}><Badge category={selected.lead_category} /></span>
              </div>
              <span style={{ fontSize: 12, color: 'var(--text2)' }}>{fmtDate(selected.timestamp)} · {fmtDuration(selected.duration_sec)}</span>
            </div>
            {selected.summary && (
              <div style={{ margin: '12px 1.25rem 0', padding: '10px 14px', background: 'var(--accent-dim)', borderLeft: '2px solid var(--accent)', borderRadius: '0 8px 8px 0', fontSize: 13 }}>
                <p style={{ fontSize: 10, textTransform: 'uppercase', color: 'var(--accent)', marginBottom: 4 }}>AI Summary</p>
                <p>{selected.summary}</p>
              </div>
            )}
            <div style={{ flex: 1, overflowY: 'auto', padding: '1rem 1.25rem' }}>
              {txLoading && <p style={{ color: 'var(--text2)', textAlign: 'center', padding: '2rem', fontSize: 13 }}>Loading transcript…</p>}
              {txData?.error && <p style={{ color: 'var(--hot)', padding: '1rem', fontSize: 13 }}>Error: {txData.error}</p>}
              {!txLoading && (!txData?.transcript || txData.transcript.length === 0) && !txData?.error && (
                <VisualEmptyState message="No transcript data available for this call" />
              )}
              {txData?.transcript && Array.isArray(txData.transcript) && txData.transcript.length > 0 && (
                <pre style={{
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  fontFamily: 'inherit',
                  fontSize: 13,
                  lineHeight: 1.6,
                  color: 'var(--text)',
                  background: 'var(--bg3)',
                  padding: '12px 14px',
                  borderRadius: 10,
                  margin: 0,
                }}>
                  {txData.transcript.map(l => `${l.role}: ${l.text}`).join('\n')}
                </pre>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
