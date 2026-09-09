// src/components/Login.jsx
import { useState } from 'react'
import { supabase } from '../supabaseClient'
import { LogIn, Eye, EyeOff } from 'lucide-react'

export default function Login() {
  const [email,    setEmail]    = useState('')
  const [password, setPassword] = useState('')
  const [showPw,   setShowPw]   = useState(false)
  const [error,    setError]    = useState('')
  const [loading,  setLoading]  = useState(false)

  async function handleLogin() {
    if (!email.trim() || !password.trim()) {
      setError('Enter email and password.')
      return
    }
    setLoading(true)
    setError('')
    const { error: authErr } = await supabase.auth.signInWithPassword({
      email: email.trim(),
      password: password.trim(),
    })
    setLoading(false)
    if (authErr) setError(authErr.message)
    // On success, the onAuthStateChange listener in App.jsx swaps to <Dashboard/>
  }

  return (
    <div style={{
      minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'var(--bg)',
    }}>
      <div style={{
        width: 340, padding: 28, background: 'var(--bg2)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
      }}>
        <h2 style={{ margin: '0 0 4px', color: 'var(--text1)', fontSize: 18 }}>Sign in</h2>
        <p style={{ margin: '0 0 20px', color: 'var(--text2)', fontSize: 13 }}>
          Inbox Infotech dashboard access
        </p>

        <input
          type="email"
          value={email}
          onChange={e => { setEmail(e.target.value); setError('') }}
          placeholder="Email"
          onKeyDown={e => e.key === 'Enter' && handleLogin()}
          style={inputStyle}
        />

        <div style={{ position: 'relative', marginTop: 10 }}>
          <input
            type={showPw ? 'text' : 'password'}
            value={password}
            onChange={e => { setPassword(e.target.value); setError('') }}
            placeholder="Password"
            onKeyDown={e => e.key === 'Enter' && handleLogin()}
            style={{ ...inputStyle, paddingRight: 36, marginTop: 0 }}
          />
          <button
            onClick={() => setShowPw(p => !p)}
            style={{
              position: 'absolute', right: 10, top: '50%', transform: 'translateY(-50%)',
              background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text2)',
              padding: 0, display: 'flex',
            }}
          >
            {showPw ? <EyeOff size={15} /> : <Eye size={15} />}
          </button>
        </div>

        {error && (
          <p style={{ color: 'var(--hot)', fontSize: 12, margin: '10px 0 0' }}>{error}</p>
        )}

        <button
          onClick={handleLogin}
          disabled={loading}
          style={{
            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
            width: '100%', marginTop: 16, padding: '10px 16px', background: 'var(--accent)',
            border: 'none', borderRadius: 8, color: '#fff', fontSize: 13, fontWeight: 600,
            cursor: loading ? 'default' : 'pointer', opacity: loading ? 0.6 : 1,
          }}
        >
          <LogIn size={14} /> {loading ? 'Signing in…' : 'Sign in'}
        </button>
      </div>
    </div>
  )
}

const inputStyle = {
  width: '100%', boxSizing: 'border-box', padding: '10px 12px', background: 'var(--bg3)',
  border: '1px solid var(--border2)', borderRadius: 8, color: 'var(--text1)', fontSize: 13,
  outline: 'none', marginTop: 10,
}