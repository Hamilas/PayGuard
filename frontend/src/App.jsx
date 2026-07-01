import { useState, useEffect, useRef } from 'react'
import Dashboard from './pages/Dashboard.jsx'
import Detect from './pages/Detect.jsx'
import About from './pages/About.jsx'

const TABS = ['Dashboard', 'Detect', 'About']
const API = '/api'

export default function App() {
  const [activeTab, setActiveTab] = useState('Dashboard')

  // Polling lives here (not inside Dashboard) so it keeps running, and fraud
  // alerts keep firing, even while you're on the Detect or About tab.
  const [health, setHealth] = useState(null)
  const [stats, setStats] = useState(null)
  const [modelInfo, setModelInfo] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdated, setLastUpdated] = useState(null)
  const [fraudRateHistory, setFraudRateHistory] = useState([])
  const [newRowIds, setNewRowIds] = useState(new Set())
  const [alerts, setAlerts] = useState([])
  const seenIdsRef = useRef(new Set())
  const initializedRef = useRef(false)

  const fetchAll = async () => {
    try {
      const [healthRes, statsRes, modelRes] = await Promise.all([
        fetch(`${API}/health`),
        fetch(`${API}/stats?limit=20`),
        fetch(`${API}/model-info`)
      ])
      if (healthRes.ok) setHealth(await healthRes.json())
      if (statsRes.ok) {
        const s = await statsRes.json()
        setStats(s)
        setFraudRateHistory(h => [...h.slice(-29), s.fraud_rate_percent])
        const fresh = new Set()
        const newFraudAlerts = []
        s.recent_predictions?.forEach(p => {
          if (!seenIdsRef.current.has(p.transaction_id)) {
            fresh.add(p.transaction_id)
            if (p.is_fraud) newFraudAlerts.push(p)
          }
          seenIdsRef.current.add(p.transaction_id)
        })
        setNewRowIds(fresh)
        if (initializedRef.current && newFraudAlerts.length > 0) {
          setAlerts(a => [
            ...newFraudAlerts.map(p => ({ id: p.transaction_id, amount: p.amount, prob: p.fraud_probability, ts: Date.now() })),
            ...a
          ].slice(0, 5))
        }
        initializedRef.current = true
      }
      if (modelRes.ok) setModelInfo(await modelRes.json())
      setError(null)
      setLastUpdated(Date.now())
    } catch (e) {
      setError('API unreachable, start Docker Compose to connect')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchAll()
    const interval = setInterval(fetchAll, 10000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    if (alerts.length === 0) return
    const t = setTimeout(() => setAlerts(a => a.slice(0, -1)), 8000)
    return () => clearTimeout(t)
  }, [alerts])

  const dashboardProps = { health, stats, modelInfo, error, loading, lastUpdated, fraudRateHistory, newRowIds, alerts, setAlerts, fetchAll }

  return (
    <div style={{ minHeight: '100vh', background: '#0a0f1a' }}>
      <style>{`
        @keyframes pg-pulse {
          0%   { box-shadow: 0 0 0 0 rgba(34,197,94,0.55); }
          70%  { box-shadow: 0 0 0 8px rgba(34,197,94,0); }
          100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); }
        }
        @keyframes pg-fade-in-row {
          from { background-color: rgba(96,165,250,0.25); opacity: 0.3; }
          to   { background-color: transparent; opacity: 1; }
        }
        @keyframes pg-spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
        .pg-live-dot { animation: pg-pulse 1.8s ease-out infinite; }
        .pg-new-row { animation: pg-fade-in-row 1.6s ease-out; }
        .pg-spinner { animation: pg-spin 0.7s linear infinite; }
      `}</style>

      {/* Fraud alert toasts, rendered at App level so they show on every tab */}
      <div style={{ position: 'fixed', top: 76, right: 20, zIndex: 200, display: 'flex', flexDirection: 'column', gap: 8, width: 300 }}>
        {alerts.map(a => (
          <div key={a.id + a.ts} className="pg-new-row" style={{
            background: '#1a0a0a', border: '1px solid #ef4444', borderRadius: 10,
            padding: '0.75rem 1rem', boxShadow: '0 4px 16px rgba(0,0,0,0.4)'
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ color: '#fca5a5', fontWeight: 700, fontSize: '0.85rem' }}>⚠ Fraud caught</span>
              <button onClick={() => setAlerts(al => al.filter(x => x.id !== a.id))} style={{ background: 'none', border: 'none', color: '#64748b', cursor: 'pointer', fontSize: '1rem', lineHeight: 1 }}>×</button>
            </div>
            <div style={{ color: '#94a3b8', fontSize: '0.75rem', marginTop: 4 }}>
              ${a.amount?.toFixed(2)} · {Math.round(a.prob * 100)}% fraud probability
            </div>
            <div style={{ color: '#475569', fontSize: '0.7rem', fontFamily: 'monospace', marginTop: 2 }}>{a.id?.slice(0, 20)}…</div>
          </div>
        ))}
      </div>

      {/* Header */}
      <header style={{
        background: '#0d1424',
        borderBottom: '1px solid #1e293b',
        padding: '0 2rem',
        display: 'flex',
        alignItems: 'center',
        gap: '2rem',
        height: '64px',
        position: 'sticky',
        top: 0,
        zIndex: 100
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <div style={{
            width: 36, height: 36, background: '#ef4444',
            borderRadius: 8, display: 'flex', alignItems: 'center', justifyContent: 'center'
          }}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 1L3 5v6c0 5.25 3.75 10.15 9 11.35C17.25 21.15 21 16.25 21 11V5L12 1z"/>
              <polyline points="9 12 11 14 15 10"/>
            </svg>
          </div>
          <span style={{ fontSize: '1.2rem', fontWeight: 700, color: '#f1f5f9', letterSpacing: '-0.5px' }}>
            Pay<span style={{ color: '#ef4444' }}>Guard</span>
          </span>
        </div>

        <nav style={{ display: 'flex', gap: '4px', marginLeft: 'auto' }}>
          {TABS.map(tab => (
            <button key={tab} onClick={() => setActiveTab(tab)} style={{
              padding: '6px 18px',
              borderRadius: 6,
              border: 'none',
              cursor: 'pointer',
              fontWeight: 500,
              fontSize: '0.9rem',
              transition: 'all 0.15s',
              background: activeTab === tab ? '#ef4444' : 'transparent',
              color: activeTab === tab ? '#fff' : '#64748b'
            }}>
              {tab}
            </button>
          ))}
        </nav>
      </header>

      {/* Content */}
      <main style={{ padding: '2rem', maxWidth: 1200, margin: '0 auto' }}>
        {activeTab === 'Dashboard' && <Dashboard {...dashboardProps} />}
        {activeTab === 'Detect' && <Detect />}
        {activeTab === 'About' && <About />}
      </main>
    </div>
  )
}
