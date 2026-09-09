// src/App.jsx
import { useState, useEffect } from 'react'
import { supabase } from './supabaseClient'
import Dashboard from './components/Dashboard'
import Login from './components/Login'

export default function App() {
  const [session, setSession] = useState(undefined) // undefined = checking, null = logged out

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => setSession(data.session))
    const { data: sub } = supabase.auth.onAuthStateChange((_event, sess) => setSession(sess))
    return () => sub.subscription.unsubscribe()
  }, [])

  if (session === undefined) return null // brief flash-guard while checking existing session
  if (!session) return <Login />
  return <Dashboard />
}