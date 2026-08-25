// src/components/PageSettings.jsx

import { useState, useEffect, useCallback } from 'react'
import PageAdminPanel from './PageAdminPanel'
import {
  Save, Plus, Trash2, ExternalLink, LogIn, LogOut,
  Eye, EyeOff, Globe, Zap, Mail, Calendar, Hash, Link2,
  MessageSquare, Users
} from 'lucide-react'

// ─── constants ────────────────────────────────────────────────────────────────

const RETENTION_OPTIONS = [
  { label: '7 days',   value: '7'   },
  { label: '30 days',  value: '30'  },
  { label: '90 days',  value: '90'  },
  { label: '180 days', value: '180' },
  { label: '1 year',   value: '365' },
  { label: 'Forever',  value: '0'   },
]

const BUILTIN_CONNECTIONS = [
  {
    key:         'conn_teams',
    label:       'Microsoft Teams',
    type:        'webhook',
    placeholder: 'https://outlook.office.com/webhook/...',
    Icon:        Users,
    color:       '#5b9cf6',
    description: 'Post lead alerts to a Teams channel',
  },
  {
    key:         'conn_slack',
    label:       'Slack',
    type:        'webhook',
    placeholder: 'https://hooks.slack.com/services/...',
    Icon:        MessageSquare,
    color:       '#4ade80',
    description: 'Post lead alerts to a Slack channel',
  },
  {
    key:         'conn_calendly',
    label:       'Calendly',
    type:        'link',
    placeholder: 'https://calendly.com/your-link',
    Icon:        Calendar,
    color:       '#f5a623',
    description: 'Booking link sent to hot leads',
  },
  {
    key:         'conn_email',
    label:       'Notification Email',
    type:        'email',
    placeholder: 'sales@yourcompany.com',
    Icon:        Mail,
    color:       '#ff6b4a',
    description: 'Receive email alerts for new leads',
  },
]

const ADMIN_KEY = 'admin_password_hash'

async function hashPassword(pw) {
  if (!pw) return ''
  const buf = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(pw)
  )
  return Array.from(new Uint8Array(buf))
    .map(b => b.toString(16).padStart(2, '0'))
    .join('')
}

// ─── main component ───────────────────────────────────────────────────────────

export default function PageSettings({ showToast, onConfigChange, supabase }) {
  const [websiteLink, setWebsiteLink] = useState('https://www.inboxtechs.com/')
  const [retention,   setRetention]   = useState('90')
  const [conns,       setConns]       = useState({})
  const [customConns, setCustomConns] = useState([])

  // admin — FIX: separate hashLoaded flag prevents comparing against uninitialised state
  const [storedHash,     setStoredHash]     = useState('')
  const [hashLoaded,     setHashLoaded]     = useState(false)
  const [adminLoggedIn,  setAdminLoggedIn]  = useState(() => sessionStorage.getItem('adminLoggedIn') === '1')
  const [showAdminPanel, setShowAdminPanel] = useState(false)
  const [adminPw,        setAdminPw]        = useState('')
  const [newAdminPw,     setNewAdminPw]     = useState('')
  const [showPw,         setShowPw]         = useState(false)
  const [loginError,     setLoginError]     = useState('')

  const [loading, setLoading] = useState(true)
  const [saving,  setSaving]  = useState(false)

  // ── load ──────────────────────────────────────────────────────────────────
  const loadConfig = useCallback(async () => {
    const keys = [
      'website_link', 'transcript_retention', ADMIN_KEY,
      ...BUILTIN_CONNECTIONS.map(c => c.key),
      'custom_connections',
    ]
    const { data, error } = await supabase
      .from('agent_config')
      .select('key, value')
      .eq('agent_id', 'default')   // global app settings live under the default agent bucket
      .in('key', keys)

    if (error) { console.error('[PageSettings load]', error.message); setLoading(false); return }

    const map = {}
    ;(data || []).forEach(r => { map[r.key] = r.value })

    if (map['website_link'])         setWebsiteLink(map['website_link'])
    if (map['transcript_retention']) setRetention(map['transcript_retention'])

    // FIX: always set both hash + loaded, even when hash is empty string
    setStoredHash(map[ADMIN_KEY] ?? '')
    setHashLoaded(true)

    const connMap = {}
    BUILTIN_CONNECTIONS.forEach(c => { connMap[c.key] = map[c.key] || '' })
    setConns(connMap)

    if (map['custom_connections']) {
      try { setCustomConns(JSON.parse(map['custom_connections'])) } catch {}
    }

    setLoading(false)
  }, [supabase])

  useEffect(() => { loadConfig() }, [loadConfig])

  // ── save ──────────────────────────────────────────────────────────────────
  async function save() {
    setSaving(true)
    const now = new Date().toISOString()
    const rows = [
      { agent_id: 'default', key: 'website_link',         value: websiteLink,                 updated_at: now },
      { agent_id: 'default', key: 'transcript_retention', value: retention,                   updated_at: now },
      { agent_id: 'default', key: 'custom_connections',   value: JSON.stringify(customConns), updated_at: now },
      ...BUILTIN_CONNECTIONS.map(c => ({
        agent_id: 'default', key: c.key, value: conns[c.key] || '', updated_at: now,
      })),
    ]

    if (newAdminPw.trim() && adminLoggedIn) {
      const hash = await hashPassword(newAdminPw.trim())
      rows.push({ agent_id: 'default', key: ADMIN_KEY, value: hash, updated_at: now })
      setStoredHash(hash)
      setNewAdminPw('')
    }

    const { error } = await supabase
      .from('agent_config')
      .upsert(rows, { onConflict: 'agent_id,key' })

    setSaving(false)
    if (error) { console.error('[PageSettings save]', error.message); showToast('Error saving settings', 'err'); return }
    showToast('Settings saved ✓')
    onConfigChange?.({ website_link: websiteLink, transcript_retention: retention, ...conns })
  }

  // ── admin login ───────────────────────────────────────────────────────────
  async function handleAdminLogin() {
    setLoginError('')
    if (!hashLoaded)         { setLoginError('Still loading — try again.'); return }
    if (!adminPw.trim())     { setLoginError('Please enter a password.');   return }

    const hash = await hashPassword(adminPw.trim())

    // first-time setup
    if (!storedHash) {
      const { error } = await supabase
        .from('agent_config')
        .upsert([{ agent_id: 'default', key: ADMIN_KEY, value: hash, updated_at: new Date().toISOString() }], { onConflict: 'agent_id,key' })
      if (error) { setLoginError('Error saving password.'); return }
      setStoredHash(hash)
      setAdminLoggedIn(true)
      sessionStorage.setItem('adminLoggedIn', '1')
      setAdminPw('')
      showToast('Admin password set ✓')
      return
    }

    // FIX: compare computed hash against storedHash (both are SHA-256 hex strings)
    if (hash === storedHash) {
      setAdminLoggedIn(true)
      sessionStorage.setItem('adminLoggedIn', '1')
      setAdminPw('')
      showToast('Logged in as admin ✓')
    } else {
      setLoginError('Incorrect password.')
    }
  }

  // ── custom connections ────────────────────────────────────────────────────
  function addCustomConn() {
    setCustomConns(prev => [...prev, { id: crypto.randomUUID(), label: '', type: 'link', value: '' }])
  }
  function updateCustomConn(id, field, val) {
    setCustomConns(prev => prev.map(c => c.id === id ? { ...c, [field]: val } : c))
  }
  function removeCustomConn(id) {
    setCustomConns(prev => prev.filter(c => c.id !== id))
  }
  function openConn(value, type) {
    if (!value) return
    let url = value
    if (type === 'email')             url = `mailto:${value}`
    else if (!url.startsWith('http')) url = `https://${url}`
    window.open(url, '_blank', 'noopener')
  }

  // ─────────────────────────────────────────────────────────────────────────

  if (loading) return (
    <p style={{ color: 'var(--text2)', padding: '2rem', textAlign: 'center' }}>Loading config…</p>
  )

  return (
    <div style={{  display: 'flex', flexDirection: 'column', gap: 20 }}>

      {/* ── General ── */}
      <Section title="General">
        <Field label="Website">
          <div style={{ maxWidth: 680,display: 'flex', gap: 8 }}>
            <SInput type="url" value={websiteLink} onChange={e => setWebsiteLink(e.target.value)} placeholder="https://www.inboxtechs.com/" />
            <IconBtn title="Open website" onClick={() => window.open(websiteLink, '_blank', 'noopener')}>
              <Globe size={15} />
            </IconBtn>
          </div>
        </Field>

        <Field label="Transcript retention">
          <select value={retention} onChange={e => setRetention(e.target.value)} style={selectStyle}>
            {RETENTION_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
          <p style={{ fontSize: 11, color: 'var(--text2)', marginTop: 5 }}>
            {retention === '0' ? 'Transcripts are kept indefinitely.' : `Transcripts older than ${RETENTION_OPTIONS.find(o => o.value === retention)?.label} will be automatically deleted.`}
          </p>
        </Field>
      </Section>

      {/* ── Connections ── */}
      <Section
        title="Connections"
        action={<button onClick={addCustomConn} style={addBtnStyle}><Plus size={13} /> Add</button>}
      >
        <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 4 }}>
          Connect your tools — click the arrow icon to open or test a connection.
        </p>

        {/* 2×2 grid */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: customConns.length > 0 ? 12 : 0 }}>
          {BUILTIN_CONNECTIONS.map(c => (
            <ConnCard
              key={c.key}
              Icon={c.Icon}
              iconColor={c.color}
              label={c.label}
              description={c.description}
              type={c.type}
              value={conns[c.key] || ''}
              placeholder={c.placeholder}
              onChange={val => setConns(prev => ({ ...prev, [c.key]: val }))}
              onOpen={() => openConn(conns[c.key], c.type)}
            />
          ))}
        </div>

        {/* custom rows */}
        {customConns.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {customConns.map(c => (
              <ConnCardCustom
                key={c.id}
                label={c.label} type={c.type} value={c.value}
                onLabelChange={val => updateCustomConn(c.id, 'label', val)}
                onTypeChange={val  => updateCustomConn(c.id, 'type',  val)}
                onChange={val      => updateCustomConn(c.id, 'value', val)}
                onOpen={() => openConn(c.value, c.type)}
                onRemove={() => removeCustomConn(c.id)}
              />
            ))}
          </div>
        )}
      </Section>

    {/* ── Admin ── */}
      <Section
        title="Admin"
        action={adminLoggedIn
          ? <button onClick={() => { setAdminLoggedIn(false); sessionStorage.removeItem('adminLoggedIn'); setShowAdminPanel(false) }} style={addBtnStyle}><LogOut size={13} /> Log out</button>
          : null}
      >
        {!adminLoggedIn ? (
          <div>
            <p style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 12 }}>
              {!hashLoaded ? 'Loading…' : storedHash ? 'Log in with your admin password.' : 'No password set yet — enter one below to create it.'}
            </p>
            <div style={{ display: 'flex', gap: 8 }}>
              <div style={{ position: 'relative', flex: 1 }}>
                <SInput
                  type={showPw ? 'text' : 'password'}
                  value={adminPw}
                  onChange={e => { setAdminPw(e.target.value); setLoginError('') }}
                  placeholder="Admin password"
                  onKeyDown={e => e.key === 'Enter' && handleAdminLogin()}
                  style={{ paddingRight: 36 }}
                />
                <button onClick={() => setShowPw(p => !p)}
                  style={{ position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)', background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text2)', padding: 0, display: 'flex' }}>
                  {showPw ? <EyeOff size={15} /> : <Eye size={15} />}
                </button>
              </div>
              <button onClick={handleAdminLogin} disabled={!hashLoaded}
                style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '9px 16px', background: 'var(--accent)', border: 'none', borderRadius: 8, color: '#fff', fontSize: 13, fontWeight: 600, cursor: hashLoaded ? 'pointer' : 'default', opacity: hashLoaded ? 1 : 0.5, whiteSpace: 'nowrap' }}>
                <LogIn size={14} /> {storedHash ? 'Log in' : 'Set password'}
              </button>
            </div>
            {loginError && (
              <p style={{ fontSize: 12, color: 'var(--hot)', marginTop: 8 }}>⚠ {loginError}</p>
            )}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px', background: 'rgba(74,222,128,0.08)', border: '0.5px solid rgba(74,222,128,0.3)', borderRadius: 8, fontSize: 13, color: '#4ade80' }}>
              <Zap size={14} /> Logged in as admin
            </div>
            <button onClick={() => setShowAdminPanel(p => !p)} style={addBtnStyle}>
              {showAdminPanel ? 'Hide' : 'Show'} admin panel
            </button>
            {showAdminPanel && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12, padding: 14, background: 'var(--bg3)', borderRadius: 10, border: '0.5px solid var(--border)' }}>
                <p style={{ fontSize: 12, fontWeight: 600, color: 'var(--text1)', margin: 0 }}>Change admin password</p>
                <SInput type="password" value={newAdminPw} onChange={e => setNewAdminPw(e.target.value)} placeholder="New password (leave blank to keep current)" />
                <p style={{ fontSize: 11, color: 'var(--text2)', margin: 0 }}>Hashed with SHA-256. Click Save Settings to apply.</p>
              </div>
            )}
          </div>
        )}
      </Section>

      {/* ── Admin DB Panel ── */}
      {adminLoggedIn && <PageAdminPanel supabase={supabase} showToast={showToast} />}

      {/* ── Save ── */}
      <button onClick={save} disabled={saving}
        style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '10px 22px', background: 'var(--accent)', border: 'none', borderRadius: 8, color: '#fff', fontSize: 13, fontWeight: 600, cursor: saving ? 'default' : 'pointer', opacity: saving ? 0.6 : 1, width: 'fit-content' }}>
        <Save size={14} /> {saving ? 'Saving…' : 'Save Settings'}
      </button>
    </div>
  )
}
// ─── ConnCard (built-in, 2×2 grid) ───────────────────────────────────────────

function ConnCard({ Icon, iconColor, label, description, type, value, placeholder, onChange, onOpen }) {
  const hasValue = Boolean(value)
  return (
    <div style={{ background: 'var(--bg2)', border: `0.5px solid ${hasValue ? 'rgba(108,99,255,0.4)' : 'var(--border)'}`, borderRadius: 12, padding: '14px', display: 'flex', flexDirection: 'column', gap: 10, position: 'relative' }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <div style={{ width: 32, height: 32, borderRadius: 8, flexShrink: 0, background: `${iconColor}18`, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <Icon size={16} color={iconColor} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ fontSize: 13, fontWeight: 600, color: 'var(--text1)', margin: '0 0 2px' }}>{label}</p>
          <p style={{ fontSize: 11, color: 'var(--text2)', margin: 0 }}>{description}</p>
        </div>
        <button onClick={onOpen} disabled={!hasValue} title="Open / test"
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: 28, height: 28, background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 7, cursor: hasValue ? 'pointer' : 'default', color: hasValue ? 'var(--accent)' : 'var(--text3)', opacity: hasValue ? 1 : 0.4, flexShrink: 0 }}>
          <ExternalLink size={12} />
        </button>
      </div>
      <input
        type={type === 'email' ? 'email' : type === 'link' ? 'url' : 'text'}
        value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}
        style={{ width: '100%', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 7, padding: '7px 10px', color: 'var(--text1)', fontSize: 12, outline: 'none', boxSizing: 'border-box' }}
      />
      {hasValue && (
        <span style={{ position: 'absolute', top: 10, right: 44, background: 'rgba(74,222,128,0.12)', color: '#4ade80', fontSize: 9, fontWeight: 700, padding: '2px 7px', borderRadius: 10, letterSpacing: 0.4, textTransform: 'uppercase' }}>
          Connected
        </span>
      )}
    </div>
  )
}

// ─── ConnCardCustom ───────────────────────────────────────────────────────────

function ConnCardCustom({ label, type, value, onLabelChange, onTypeChange, onChange, onOpen, onRemove }) {
  const TypeIcon = type === 'email' ? Mail : type === 'webhook' ? Hash : Link2
  return (
    <div style={{ background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 12, padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{ width: 28, height: 28, borderRadius: 7, flexShrink: 0, background: 'var(--bg3)', border: '0.5px solid var(--border)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <TypeIcon size={14} color="var(--text2)" />
        </div>
        <input value={label} onChange={e => onLabelChange(e.target.value)} placeholder="Connection name"
          style={{ flex: 1, background: 'none', border: 'none', outline: 'none', fontSize: 13, fontWeight: 600, color: 'var(--text1)' }} />
        <select value={type} onChange={e => onTypeChange(e.target.value)}
          style={{ ...selectStyle, padding: '4px 8px', fontSize: 11, width: 'auto' }}>
          <option value="link">Link</option>
          <option value="webhook">Webhook</option>
          <option value="email">Email</option>
        </select>
        <button onClick={onOpen} disabled={!value} title="Open / test"
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: 28, height: 28, background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 7, cursor: value ? 'pointer' : 'default', color: value ? 'var(--accent)' : 'var(--text3)', opacity: value ? 1 : 0.4 }}>
          <ExternalLink size={12} />
        </button>
        <button onClick={onRemove} title="Remove"
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: 28, height: 28, background: 'none', border: 'none', borderRadius: 7, cursor: 'pointer', color: 'var(--hot)' }}>
          <Trash2 size={13} />
        </button>
      </div>
      <input
        type={type === 'email' ? 'email' : type === 'link' ? 'url' : 'text'}
        value={value} onChange={e => onChange(e.target.value)}
        placeholder={type === 'email' ? 'someone@example.com' : 'https://…'}
        style={{ width: '100%', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 7, padding: '7px 10px', color: 'var(--text1)', fontSize: 12, outline: 'none', boxSizing: 'border-box' }}
      />
    </div>
  )
}

// ─── layout helpers ───────────────────────────────────────────────────────────

function Section({ title, children, action }) {
  return (
    <div style={{ background: 'var(--bg2)', border: '0.5px solid var(--border)', borderRadius: 14, padding: '1.25rem 1.5rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18 }}>
        <h3 style={{ fontSize: 14, fontWeight: 600, margin: 0 }}>{title}</h3>
        {action}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>{children}</div>
    </div>
  )
}

function Field({ label, children }) {
  return (
    <div>
      <label style={{ display: 'block', fontSize: 11, color: 'var(--text2)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: 0.5 }}>{label}</label>
      {children}
    </div>
  )
}

function SInput({ style: extra, ...props }) {
  return (
    <input {...props} style={{ width: '100%', background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, padding: '8px 12px', color: 'var(--text1)', fontSize: 13, outline: 'none', boxSizing: 'border-box', ...extra }} />
  )
}

function IconBtn({ children, ...props }) {
  return (
    <button {...props} style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: 36, height: 36, flexShrink: 0, background: 'var(--bg3)', border: '0.5px solid var(--border)', borderRadius: 8, cursor: 'pointer', color: 'var(--text2)' }}>
      {children}
    </button>
  )
}

const selectStyle = {
  background: 'var(--bg3)', border: '0.5px solid var(--border)',
  borderRadius: 8, padding: '8px 12px', color: 'var(--text1)', fontSize: 13, outline: 'none', width: '100%',
}

const addBtnStyle = {
  display: 'flex', alignItems: 'center', gap: 4,
  padding: '5px 12px', background: 'var(--bg3)', border: '0.5px solid var(--border)',
  borderRadius: 8, color: 'var(--text2)', fontSize: 12, fontWeight: 600, cursor: 'pointer',
}
