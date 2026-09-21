// src/supabaseClient.js
// Single Supabase client instance — import this everywhere
import { createClient } from '@supabase/supabase-js'

const SUPABASE_URL  = import.meta.env.VITE_SUPABASE_URL
const SUPABASE_PUBLISHABLE_KEY = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY

if (!SUPABASE_URL || !SUPABASE_PUBLISHABLE_KEY) {
  throw new Error('Missing VITE_SUPABASE_URL or VITE_SUPABASE_PUBLISHABLE_KEY in .env')
}

export const supabase = createClient(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)

// NEW — root cause of "Missing or invalid Authorization header" / 401
// unauthorized on campaigns + outbound-call: backend's require_user()
// expects a Supabase Bearer token, but no fetch() call anywhere in this
// frontend ever attached one. Central helper so every backend call can
// pull it in consistently.
export async function authHeader() {
  const { data } = await supabase.auth.getSession()
  const token = data?.session?.access_token
  return token ? { Authorization: `Bearer ${token}` } : {}
}