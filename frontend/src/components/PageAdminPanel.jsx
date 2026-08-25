// src/components/PageAdminPanel.jsx
import React, { useEffect, useMemo, useState, useCallback, useRef } from 'react'
import {
  Database, RefreshCw, Search, Download,
  Trash2, Save, X, Plus, ChevronLeft, ChevronRight,
  AlertCircle, CheckCircle2, Edit3
} from 'lucide-react'

// ─── schema ───────────────────────────────────────────────────────────────────

// Every table the app writes to, grouped for the sidebar. If a table
// exists in Supabase but isn't listed here, it won't show up in the panel —
// keep this in sync with migration_multi_agent.sql.
const CATEGORIES = [
  { label: 'Agents',       tables: ['agents', 'agent_numbers', 'agent_config', 'prompt_versions'] },
  { label: 'Leads & Calls', tables: ['calls', 'lead_notes'] },
  { label: 'Campaigns',    tables: ['campaigns', 'campaign_leads', 'call_attempts'] },
  { label: 'Forms',        tables: ['forms', 'form_submissions', 'form_send_log'] },
]

const TABLES = {
  agents: {
    pk: 'agent_id',
    readonly: ['created_at', 'updated_at'],
    columns: ['agent_id', 'name', 'phone_number', 'is_active', 'created_at', 'updated_at'],
    types: { is_active: 'boolean' },
  },
  agent_numbers: {
    // Plivo number → agent routing. number is the PK, so one number can
    // only ever belong to one agent — an agent can still own several rows.
    pk: 'number',
    readonly: ['assigned_at'],
    columns: ['number', 'agent_id', 'region', 'assigned_at'],
    types: {},
  },
  agent_config: {
    // composite key — one agent can have many keys (system_prompt, etc),
    // and the same key ('system_prompt') repeats across agents, so
    // uniqueness is (agent_id, key) together, not either column alone.
    pk: ['agent_id', 'key'],
    readonly: ['updated_at'],
    columns: ['agent_id', 'key', 'value', 'updated_at'],
    types: { key: 'text', value: 'text', updated_at: 'timestamptz' },
  },
  prompt_versions: {
    pk: 'id',
    readonly: ['id', 'created_at'],
    columns: ['id', 'agent_id', 'prompt_key', 'prompt_value', 'rollback_note', 'created_at'],
    types: {},
  },
  calls: {
    pk: 'id',
    readonly: ['id', 'created_at'],
    columns: [
      'id', 'call_sid', 'from_number', 'to_number', 'duration_sec',
      'transcript', 'lead_category', 'lead_score', 'extracted',
      'recording_url', 'source', 'name', 'company', 'agent_id',
      'live_facts', 'live_outcome', 'live_history', 'live_updated_at',
      'last_contacted_at', 'created_at',
    ],
    types: {
      extracted: 'jsonb', live_facts: 'jsonb', live_history: 'jsonb',
      duration_sec: 'number', lead_score: 'number',
    },
  },
  lead_notes: {
    pk: 'id',
    readonly: ['id', 'created_at'],
    columns: ['id', 'call_sid', 'note', 'author', 'created_at'],
    types: {},
  },
  // ── campaigns — durable batch-calling state ────────────────────
  campaigns: {
    pk: 'campaign_id',
    readonly: ['campaign_id', 'created_at'],
    columns: ['campaign_id', 'agent_id', 'file_name', 'total_leads', 'status', 'created_at'],
    types: { total_leads: 'number' },
  },
  campaign_leads: {
    pk: 'lead_id',
    readonly: ['lead_id', 'campaign_id'],
    columns: ['lead_id', 'campaign_id', 'row_index', 'phone', 'name', 'raw_row', 'status'],
    types: { raw_row: 'jsonb', row_index: 'number' },
  },
  call_attempts: {
    pk: 'attempt_id',
    // idempotency_key is what makes duplicate-call prevention work —
    // editing it by hand would break the uniqueness it relies on, so
    // it's readonly here same as the id/timestamp columns.
    readonly: ['attempt_id', 'lead_id', 'campaign_id', 'idempotency_key', 'started_at'],
    columns: [
      'attempt_id', 'lead_id', 'campaign_id', 'attempt_number', 'idempotency_key',
      'call_uuid', 'provider_status', 'hangup_cause', 'business_status',
      'started_at', 'ended_at', 'failure_reason',
    ],
    types: { attempt_number: 'number' },
  },
  forms: {
    pk: 'id',
    readonly: ['id', 'created_at'],
    columns: ['id', 'form_url', 'label', 'gmail', 'created_by', 'last_used_at', 'created_at'],
    types: {},
  },
  form_submissions: {
    pk: 'id',
    readonly: ['id', 'submitted_at'],
    columns: ['id', 'name', 'to_number', 'email', 'service_requirements', 'budget', 'timeline', 'submitted_at'],
    types: {},
  },
  form_send_log: {
    pk: 'id',
    readonly: ['id', 'sent_at'],
    columns: ['id', 'lead_name', 'lead_email', 'sent_by', 'form_url', 'sent_at', 'response_received'],
    types: { response_received: 'boolean' },
  },
}

const GLOBAL_READONLY = new Set(['id', 'created_at', 'updated_at', 'submitted_at', 'sent_at'])
const PAGE_SIZE = 20

// ─── helpers ──────────────────────────────────────────────────────────────────

function isReadonly(tableName, col) {
  const extra = TABLES[tableName]?.readonly || []
  return GLOBAL_READONLY.has(col) || extra.includes(col)
}

function isJsonb(tableName, col) {
  return TABLES[tableName]?.types?.[col] === 'jsonb'
}

function isNumber(tableName, col) {
  return TABLES[tableName]?.types?.[col] === 'number'
}

function isBoolean(tableName, col) {
  return TABLES[tableName]?.types?.[col] === 'boolean'
}

function displayVal(v) {
  if (v === null || v === undefined) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function truncate(str, n = 60) {
  if (!str) return ''
  return str.length > n ? str.slice(0, n) + '…' : str
}

// ─── styles ───────────────────────────────────────────────────────────────────

const S = {
  wrap: {
    marginTop: 20,
    display: 'flex',
    gap: 14,
    minHeight: 520,
  },
  sidebar: {
    width: 200,
    flexShrink: 0,
    background: 'var(--bg2)',
    border: '0.5px solid var(--border)',
    borderRadius: 12,
    padding: 10,
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    overflowY: 'auto',
  },
  sidebarLabel: {
    fontSize: 10,
    fontWeight: 700,
    color: 'var(--text3)',
    textTransform: 'uppercase',
    letterSpacing: 0.8,
    padding: '4px 6px 10px',
  },
  categoryLabel: {
    fontSize: 9,
    fontWeight: 700,
    color: 'var(--text3)',
    textTransform: 'uppercase',
    letterSpacing: 0.7,
    padding: '12px 6px 4px',
  },
  countBadge: (active) => ({
    fontSize: 10,
    fontWeight: 600,
    padding: '1px 6px',
    borderRadius: 20,
    background: active ? 'rgba(255,255,255,0.2)' : 'var(--bg3)',
    color: active ? '#fff' : 'var(--text3)',
    flexShrink: 0,
  }),
  tableBtn: (active) => ({
    width: '100%',
    padding: '9px 11px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 8,
    textAlign: 'left',
    background: active ? 'var(--accent)' : 'transparent',
    border: active ? 'none' : '0.5px solid transparent',
    borderRadius: 8,
    color: active ? '#fff' : 'var(--text2)',
    fontSize: 12,
    fontWeight: active ? 600 : 400,
    cursor: 'pointer',
    transition: 'all .15s',
    whiteSpace: 'nowrap',
  }),
  main: {
    flex: 1,
    background: 'var(--bg2)',
    border: '0.5px solid var(--border)',
    borderRadius: 12,
    padding: 16,
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
    minWidth: 0,
  },
  toolbar: {
    display: 'flex',
    gap: 8,
    alignItems: 'center',
    flexWrap: 'wrap',
  },
  searchWrap: {
    flex: 1,
    minWidth: 140,
    position: 'relative',
    display: 'flex',
    alignItems: 'center',
  },
  searchIcon: {
    position: 'absolute',
    left: 9,
    color: 'var(--text3)',
    pointerEvents: 'none',
  },
  searchInput: {
    width: '100%',
    background: 'var(--bg3)',
    border: '0.5px solid var(--border)',
    borderRadius: 8,
    padding: '7px 10px 7px 30px',
    color: 'var(--text1)',
    fontSize: 12,
    outline: 'none',
    boxSizing: 'border-box',
  },
  iconBtn: (danger) => ({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 5,
    padding: '7px 11px',
    background: danger ? 'rgba(255,80,80,.12)' : 'var(--bg3)',
    border: `0.5px solid ${danger ? 'rgba(255,80,80,.3)' : 'var(--border)'}`,
    borderRadius: 8,
    color: danger ? 'var(--hot)' : 'var(--text2)',
    fontSize: 12,
    cursor: 'pointer',
    flexShrink: 0,
    whiteSpace: 'nowrap',
  }),
  meta: {
    fontSize: 11,
    color: 'var(--text3)',
    marginLeft: 'auto',
    flexShrink: 0,
  },
  errorBanner: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: 8,
    background: 'rgba(255,80,80,.1)',
    border: '0.5px solid rgba(255,80,80,.35)',
    borderRadius: 8,
    padding: '9px 12px',
    fontSize: 12,
    color: 'var(--hot)',
  },
  tableWrap: {
    overflow: 'auto',
    flex: 1,
    borderRadius: 8,
    border: '0.5px solid var(--border)',
  },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: 12,
  },
  th: {
    textAlign: 'left',
    padding: '8px 10px',
    background: 'var(--bg3)',
    color: 'var(--text3)',
    fontSize: 10,
    fontWeight: 700,
    textTransform: 'uppercase',
    letterSpacing: 0.6,
    borderBottom: '0.5px solid var(--border)',
    whiteSpace: 'nowrap',
    position: 'sticky',
    top: 0,
    zIndex: 2,
  },
  td: (ro) => ({
    padding: '6px 10px',
    borderBottom: '0.5px solid var(--border)',
    color: ro ? 'var(--text3)' : 'var(--text1)',
    maxWidth: 240,
    verticalAlign: 'middle',
  }),
  cellDiv: (ro) => ({
    cursor: ro ? 'default' : 'pointer',
    borderRadius: 4,
    padding: '2px 4px',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    maxWidth: 230,
    opacity: ro ? 0.55 : 1,
    transition: 'background .12s',
  }),
  cellInput: {
    width: '100%',
    background: 'var(--bg3)',
    border: '1px solid var(--accent)',
    borderRadius: 5,
    padding: '3px 6px',
    color: 'var(--text1)',
    fontSize: 12,
    outline: 'none',
    boxSizing: 'border-box',
  },
  cellTextarea: {
    width: '100%',
    minWidth: 200,
    minHeight: 80,
    background: 'var(--bg3)',
    border: '1px solid var(--accent)',
    borderRadius: 5,
    padding: '4px 6px',
    color: 'var(--text1)',
    fontSize: 11,
    fontFamily: 'monospace',
    outline: 'none',
    boxSizing: 'border-box',
    resize: 'both',
  },
  actionCell: {
    padding: '4px 8px',
    borderBottom: '0.5px solid var(--border)',
    whiteSpace: 'nowrap',
    verticalAlign: 'middle',
  },
  miniBtn: (hot) => ({
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: '4px 7px',
    gap: 4,
    background: hot ? 'rgba(255,80,80,.12)' : 'var(--bg3)',
    border: `0.5px solid ${hot ? 'rgba(255,80,80,.35)' : 'var(--border)'}`,
    borderRadius: 6,
    color: hot ? 'var(--hot)' : 'var(--text2)',
    fontSize: 11,
    cursor: 'pointer',
    marginRight: 4,
  }),
  pagination: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    justifyContent: 'flex-end',
    fontSize: 12,
    color: 'var(--text2)',
    paddingTop: 4,
    flexShrink: 0,
  },
  pageBtn: (disabled) => ({
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 28,
    height: 28,
    background: 'var(--bg3)',
    border: '0.5px solid var(--border)',
    borderRadius: 7,
    color: disabled ? 'var(--text3)' : 'var(--text1)',
    cursor: disabled ? 'default' : 'pointer',
    opacity: disabled ? 0.4 : 1,
  }),
  insertPanel: {
    background: 'var(--bg3)',
    border: '0.5px solid var(--border)',
    borderRadius: 10,
    padding: 14,
    display: 'flex',
    flexDirection: 'column',
    gap: 10,
    flexShrink: 0,
  },
  insertHeader: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBottom: 4,
  },
  insertTitle: {
    fontSize: 12,
    fontWeight: 700,
    color: 'var(--text1)',
  },
  insertGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
    gap: 8,
  },
  insertFieldWrap: {
    display: 'flex',
    flexDirection: 'column',
    gap: 3,
  },
  insertLabel: {
    fontSize: 10,
    color: 'var(--text3)',
    textTransform: 'uppercase',
    letterSpacing: 0.5,
  },
  insertInput: {
    background: 'var(--bg2)',
    border: '0.5px solid var(--border)',
    borderRadius: 7,
    padding: '6px 9px',
    color: 'var(--text1)',
    fontSize: 12,
    outline: 'none',
    width: '100%',
    boxSizing: 'border-box',
  },
  insertFooter: {
    display: 'flex',
    gap: 8,
  },
  saveBtn: {
    display: 'flex',
    alignItems: 'center',
    gap: 5,
    padding: '7px 14px',
    background: 'var(--accent)',
    border: 'none',
    borderRadius: 8,
    color: '#fff',
    fontSize: 12,
    fontWeight: 600,
    cursor: 'pointer',
  },
  cancelBtn: {
    display: 'flex',
    alignItems: 'center',
    gap: 5,
    padding: '7px 14px',
    background: 'var(--bg2)',
    border: '0.5px solid var(--border)',
    borderRadius: 8,
    color: 'var(--text2)',
    fontSize: 12,
    cursor: 'pointer',
  },
  jsonError: {
    fontSize: 10,
    color: 'var(--hot)',
    marginTop: 2,
  },
  loading: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 40,
    color: 'var(--text3)',
    fontSize: 13,
    gap: 8,
  },
  emptyRow: {
    textAlign: 'center',
    padding: '28px 0',
    color: 'var(--text3)',
    fontSize: 12,
  },
}

// ─── CellEditor ───────────────────────────────────────────────────────────────

function CellEditor({ value, jsonb, onSave, onCancel }) {
  const [v, setV] = useState(value)
  const [jsonErr, setJsonErr] = useState('')
  const ref = useRef(null)

  useEffect(() => { ref.current?.focus() }, [])

  function validate() {
    if (jsonb) {
      try { JSON.parse(v); setJsonErr('') } catch { setJsonErr('Invalid JSON'); return false }
    }
    return true
  }

  function handleKey(e) {
    if (!jsonb && e.key === 'Enter') { if (validate()) onSave(v) }
    if (e.key === 'Escape') onCancel()
    e.stopPropagation()
  }

  if (jsonb) {
    return (
      <div>
        <textarea
          ref={ref}
          value={v}
          onChange={e => { setV(e.target.value); setJsonErr('') }}
          onKeyDown={handleKey}
          style={S.cellTextarea}
        />
        {jsonErr && <div style={S.jsonError}>{jsonErr}</div>}
        <div style={{ display: 'flex', gap: 5, marginTop: 4 }}>
          <button style={S.miniBtn(false)} onClick={() => { if (validate()) onSave(v) }}>
            <CheckCircle2 size={11} /> Save
          </button>
          <button style={S.miniBtn(false)} onClick={onCancel}><X size={11} /></button>
        </div>
      </div>
    )
  }

  return (
    <input
      ref={ref}
      value={v}
      onChange={e => setV(e.target.value)}
      onKeyDown={handleKey}
      onBlur={() => onSave(v)}
      style={S.cellInput}
    />
  )
}

// ─── main component ───────────────────────────────────────────────────────────

export default function PageAdminPanel({ supabase, showToast }) {
  const [table, setTable]         = useState('agents')
  const [rows, setRows]           = useState([])
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState('')
  const [search, setSearch]       = useState('')
  const [page, setPage]           = useState(1)
  const [editing, setEditing]     = useState(null)   // { rowIdx, col }
  const [deleteConfirm, setDeleteConfirm] = useState(null)  // rowIdx
  const [showInsert, setShowInsert] = useState(false)
  const [insertData, setInsertData] = useState({})
  const [insertErrors, setInsertErrors] = useState({})
  const [counts, setCounts] = useState({})   // { tableName: rowCount } — sidebar badges

  // ── row counts for every table, shown as sidebar badges ─────────────────────

  const refreshCount = useCallback(async (t) => {
    const { count } = await supabase.from(t).select('*', { count: 'exact', head: true })
    setCounts(prev => ({ ...prev, [t]: count ?? 0 }))
  }, [supabase])

  useEffect(() => {
    Object.keys(TABLES).forEach(t => { refreshCount(t) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── fetch ──────────────────────────────────────────────────────────────────

  const loadRows = useCallback(async () => {
    setLoading(true)
    setError('')
    setEditing(null)
    setDeleteConfirm(null)
    try {
      const { data, error: e } = await supabase.from(table).select('*').limit(2000)
      if (e) throw e
      setRows(data || [])
      setPage(1)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [table, supabase])

  useEffect(() => { loadRows() }, [loadRows])

  // ── computed ───────────────────────────────────────────────────────────────

  const schema    = TABLES[table] || {}
  const pk        = schema.pk || 'id'
  const schemaCols = schema.columns || []

  // columns from actual data OR schema (whichever is available)
  const columns = useMemo(() => {
    if (rows.length > 0) {
      // preserve schema order, add any extra cols from actual data at end
      const fromData = Object.keys(rows[0] || {})
      const ordered  = schemaCols.filter(c => fromData.includes(c))
      const extra    = fromData.filter(c => !ordered.includes(c))
      return [...ordered, ...extra]
    }
    return schemaCols
  }, [rows, schemaCols])

  const filtered = useMemo(() => {
    if (!search.trim()) return rows
    const q = search.toLowerCase()
    return rows.filter(r =>
      Object.values(r).some(v =>
        v !== null && v !== undefined && String(v).toLowerCase().includes(q)
      )
    )
  }, [rows, search])

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE))
  const pageRows   = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  // pk can be a single column name ('id') or an array of columns
  // (e.g. ['agent_id', 'key'] for agent_config, which is keyed per-agent)
  // — this builds the right .match() filter either way.
  const pkMatch = (row) => {
    const cols = Array.isArray(pk) ? pk : [pk]
    const m = {}
    cols.forEach(c => { m[c] = row[c] })
    return m
  }

  // ── save cell ─────────────────────────────────────────────────────────────

  const saveCell = async (row, col, rawVal) => {
    setEditing(null)
    let value = rawVal
    try {
      if (isJsonb(table, col))   value = JSON.parse(rawVal)
      if (isNumber(table, col))  value = Number(rawVal)
      if (isBoolean(table, col)) value = rawVal === 'true' || rawVal === true

      const { error: e } = await supabase
        .from(table)
        .update({ [col]: value })
        .match(pkMatch(row))

      if (e) throw e
      showToast?.('Saved ✓')
      loadRows()
    } catch (e) {
      setError(e.message)
    }
  }

  // ── delete ─────────────────────────────────────────────────────────────────

  const deleteRow = async (row) => {
    setDeleteConfirm(null)
    try {
      const { error: e } = await supabase.from(table).delete().match(pkMatch(row))
      if (e) throw e
      showToast?.('Row deleted')
      loadRows()
      refreshCount(table)
    } catch (e) {
      setError(e.message)
    }
  }

  // ── insert ─────────────────────────────────────────────────────────────────

  const insertRow = async () => {
    const errs = {}
    const payload = {}

    schemaCols
      .filter(c => !isReadonly(table, c) && !GLOBAL_READONLY.has(c))
      .forEach(c => {
        const v = insertData[c]
        if (!v && v !== 0) return   // skip blanks (let DB default)
        if (isJsonb(table, c)) {
          try { payload[c] = JSON.parse(v) } catch { errs[c] = 'Invalid JSON' }
        } else if (isNumber(table, c)) {
          payload[c] = Number(v)
        } else if (isBoolean(table, c)) {
          payload[c] = v === 'true' || v === true
        } else {
          payload[c] = v
        }
      })

    if (Object.keys(errs).length) { setInsertErrors(errs); return }

    try {
      const { error: e } = await supabase.from(table).insert(payload)
      if (e) throw e
      showToast?.('Row inserted ✓')
      setShowInsert(false)
      setInsertData({})
      setInsertErrors({})
      loadRows()
      refreshCount(table)
    } catch (e) {
      setError(e.message)
    }
  }

  // ── export CSV ─────────────────────────────────────────────────────────────

  const exportCSV = () => {
    if (!filtered.length) return
    const header = columns.join(',')
    const body = filtered.map(r =>
      columns.map(c => JSON.stringify(r[c] ?? '')).join(',')
    ).join('\n')
    const blob = new Blob([header + '\n' + body], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${table}_${Date.now()}.csv`
    a.click()
  }

  // ── insert field cols (skip pure readonly) ─────────────────────────────────
  const insertCols = schemaCols.filter(c => !GLOBAL_READONLY.has(c) && !isReadonly(table, c))

  // ── render ─────────────────────────────────────────────────────────────────

  return (
    <div style={S.wrap}>

      {/* sidebar */}
      <div style={S.sidebar}>
        <div style={S.sidebarLabel}><Database size={11} style={{ marginRight: 4, verticalAlign: 'middle' }} />Database</div>
        {CATEGORIES.map(cat => (
          <div key={cat.label}>
            <div style={S.categoryLabel}>{cat.label}</div>
            {cat.tables.map(t => (
              <button key={t} style={S.tableBtn(t === table)}
                onClick={() => { setTable(t); setSearch(''); setShowInsert(false); setInsertData({}) }}>
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{t}</span>
                <span style={S.countBadge(t === table)}>{counts[t] ?? '·'}</span>
              </button>
            ))}
          </div>
        ))}
      </div>

      {/* main panel */}
      <div style={S.main}>

        {/* error */}
        {error && (
          <div style={S.errorBanner}>
            <AlertCircle size={14} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>{error}</span>
            <button onClick={() => setError('')} style={{ marginLeft: 'auto', background: 'none', border: 'none', color: 'var(--hot)', cursor: 'pointer', padding: 0 }}><X size={13} /></button>
          </div>
        )}

        {/* toolbar */}
        <div style={S.toolbar}>
          <div style={S.searchWrap}>
            <Search size={13} style={S.searchIcon} />
            <input
              value={search}
              onChange={e => { setSearch(e.target.value); setPage(1) }}
              placeholder={`Search ${table}…`}
              style={S.searchInput}
            />
          </div>
          <button style={S.iconBtn(false)} onClick={() => { loadRows(); refreshCount(table) }} title="Refresh">
            <RefreshCw size={13} />
          </button>
          <button style={S.iconBtn(false)} onClick={exportCSV} title="Export CSV" disabled={!filtered.length}>
            <Download size={13} /> CSV
          </button>
          <button style={{ ...S.iconBtn(false), color: 'var(--accent)', borderColor: 'var(--accent)' }}
            onClick={() => { setShowInsert(p => !p); setInsertData({}); setInsertErrors({}) }}>
            <Plus size={13} /> Insert
          </button>
          <span style={S.meta}>
            {filtered.length !== rows.length
              ? `${filtered.length} / ${rows.length} rows`
              : `${rows.length} rows`}
          </span>
        </div>

        {/* insert panel */}
        {showInsert && (
          <div style={S.insertPanel}>
            <div style={S.insertHeader}>
              <span style={S.insertTitle}>Insert row into <code style={{ fontSize: 11 }}>{table}</code></span>
              <button style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text3)' }}
                onClick={() => { setShowInsert(false); setInsertData({}); setInsertErrors({}) }}>
                <X size={14} />
              </button>
            </div>
            <div style={S.insertGrid}>
              {insertCols.map(col => (
                <div key={col} style={S.insertFieldWrap}>
                  <label style={S.insertLabel}>
                    {col}
                    {isJsonb(table, col) && <span style={{ color: 'var(--accent)', marginLeft: 4 }}>JSON</span>}
                  </label>
                  {isJsonb(table, col) ? (
                    <textarea
                      value={insertData[col] || ''}
                      onChange={e => { setInsertData(p => ({ ...p, [col]: e.target.value })); setInsertErrors(p => ({ ...p, [col]: '' })) }}
                      placeholder='{"key":"value"}'
                      style={{ ...S.insertInput, minHeight: 60, fontFamily: 'monospace', fontSize: 11, resize: 'vertical' }}
                    />
                  ) : isBoolean(table, col) ? (
                    <select
                      value={insertData[col] ?? 'true'}
                      onChange={e => setInsertData(p => ({ ...p, [col]: e.target.value }))}
                      style={S.insertInput}
                    >
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </select>
                  ) : (
                    <input
                      type={isNumber(table, col) ? 'number' : 'text'}
                      value={insertData[col] || ''}
                      onChange={e => setInsertData(p => ({ ...p, [col]: e.target.value }))}
                      placeholder={col}
                      style={S.insertInput}
                    />
                  )}
                  {insertErrors[col] && <span style={S.jsonError}>{insertErrors[col]}</span>}
                </div>
              ))}
            </div>
            <div style={S.insertFooter}>
              <button style={S.saveBtn} onClick={insertRow}><Save size={13} /> Insert row</button>
              <button style={S.cancelBtn} onClick={() => { setShowInsert(false); setInsertData({}); setInsertErrors({}) }}>Cancel</button>
            </div>
          </div>
        )}

        {/* table */}
        <div style={S.tableWrap}>
          {loading ? (
            <div style={S.loading}>
              <RefreshCw size={15} style={{ animation: 'spin 1s linear infinite' }} /> Loading…
              <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
            </div>
          ) : (
            <table style={S.table}>
              <thead>
                <tr>
                  {columns.map(c => <th key={c} style={S.th}>{c}</th>)}
                  <th style={{ ...S.th, width: 80 }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.length === 0 ? (
                  <tr>
                    <td colSpan={columns.length + 1} style={S.emptyRow}>
                      {search ? 'No matching rows.' : 'Table is empty.'}
                    </td>
                  </tr>
                ) : pageRows.map((row, ri) => {
                  const globalIdx = (page - 1) * PAGE_SIZE + ri
                  return (
                    <tr key={globalIdx}
                      style={{ background: ri % 2 === 0 ? 'transparent' : 'rgba(255,255,255,.018)' }}>

                      {columns.map(col => {
                        const ro   = isReadonly(table, col)
                        const jsonb = isJsonb(table, col)
                        const isActive = editing?.rowIdx === globalIdx && editing?.col === col

                        return (
                          <td key={col} style={S.td(ro)}>
                            {isActive ? (
                              <CellEditor
                                value={jsonb
                                  ? (typeof row[col] === 'object' ? JSON.stringify(row[col], null, 2) : String(row[col] ?? ''))
                                  : String(row[col] ?? '')}
                                jsonb={jsonb}
                                onSave={val => saveCell(row, col, val)}
                                onCancel={() => setEditing(null)}
                              />
                            ) : (
                              <div
                                style={S.cellDiv(ro)}
                                title={ro ? undefined : 'Click to edit'}
                                onClick={() => {
                                  if (ro) return
                                  setEditing({ rowIdx: globalIdx, col })
                                }}
                              >
                                {truncate(displayVal(row[col]))}
                              </div>
                            )}
                          </td>
                        )
                      })}

                      {/* actions */}
                      <td style={S.actionCell}>
                        {deleteConfirm === globalIdx ? (
                          <>
                            <button style={S.miniBtn(true)} onClick={() => deleteRow(row)}>
                              <CheckCircle2 size={11} /> Yes
                            </button>
                            <button style={S.miniBtn(false)} onClick={() => setDeleteConfirm(null)}>
                              <X size={11} />
                            </button>
                          </>
                        ) : (
                          <button style={S.miniBtn(true)} onClick={() => setDeleteConfirm(globalIdx)}
                            title="Delete row">
                            <Trash2 size={11} />
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* pagination */}
        {!loading && filtered.length > PAGE_SIZE && (
          <div style={S.pagination}>
            <button style={S.pageBtn(page <= 1)} onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1}>
              <ChevronLeft size={14} />
            </button>
            <span>Page {page} of {totalPages}</span>
            <button style={S.pageBtn(page >= totalPages)} onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page >= totalPages}>
              <ChevronRight size={14} />
            </button>
          </div>
        )}

      </div>
    </div>
  )
}