import { useState, useEffect } from 'react'

function Sparkline({ points, color = '#ef4444', width = 160, height = 36 }) {
  if (!points || points.length < 2) return <div style={{ height, color: '#334155', fontSize: '0.7rem' }}>collecting data…</div>
  const max = Math.max(...points, 0.01)
  const min = Math.min(...points, 0)
  const range = max - min || 1
  const step = width / (points.length - 1)
  const coords = points.map((p, i) => `${(i * step).toFixed(1)},${(height - ((p - min) / range) * height).toFixed(1)}`).join(' ')
  return (
    <svg width={width} height={height} style={{ display: 'block' }}>
      <polyline points={coords} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

function Card({ children, style = {} }) {
  return (
    <div style={{
      background: '#0d1424',
      border: '1px solid #1e293b',
      borderRadius: 12,
      padding: '1.5rem',
      ...style
    }}>
      {children}
    </div>
  )
}

function StatCard({ label, value, color = '#ef4444', sub }) {
  return (
    <Card>
      <div style={{ fontSize: '0.75rem', color: '#64748b', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 8 }}>{label}</div>
      <div style={{ fontSize: '2rem', fontWeight: 700, color, marginBottom: 4 }}>{value}</div>
      {sub && <div style={{ fontSize: '0.8rem', color: '#475569' }}>{sub}</div>}
    </Card>
  )
}

export default function Dashboard({ health, stats, modelInfo, error, loading, lastUpdated, fraudRateHistory, newRowIds, fetchAll }) {
  const [nowTick, setNowTick] = useState(Date.now())

  useEffect(() => {
    const clock = setInterval(() => setNowTick(Date.now()), 1000)
    return () => clearInterval(clock)
  }, [])

  const secsAgo = lastUpdated ? Math.max(0, Math.round((nowTick - lastUpdated) / 1000)) : null

  const apiOnline = health?.status === 'healthy'
  const fraudRate = stats ? `${stats.fraud_rate_percent}%` : '—'
  const totalScored = stats ? stats.total_scored.toLocaleString() : '—'
  const avgLatency = stats ? `${stats.avg_inference_time_ms.toFixed(2)} ms` : '—'
  const modelVersion = health?.model_version || '—'

  return (
    <div>
      <div style={{ marginBottom: '1.5rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#f1f5f9' }}>Dashboard</h1>
        <p style={{ color: '#64748b', marginTop: 4 }}>Real-time fraud detection metrics, auto-refreshes every 10s</p>
      </div>

      {error && (
        <div style={{ background: '#1a0a0a', border: '1px solid #7f1d1d', borderRadius: 8, padding: '1rem', marginBottom: '1.5rem', color: '#fca5a5' }}>
          {error}
        </div>
      )}

      {/* Status Bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: '1.5rem', flexWrap: 'wrap' }}>
        <div className={apiOnline ? 'pg-live-dot' : ''} style={{ width: 10, height: 10, borderRadius: '50%', background: apiOnline ? '#22c55e' : '#64748b' }} />
        <span style={{ color: apiOnline ? '#22c55e' : '#64748b', fontSize: '0.875rem', fontWeight: 500 }}>
          API {apiOnline ? 'Online' : 'Offline'}
        </span>
        {health && <span style={{ color: '#475569', fontSize: '0.8rem' }}>· Model v{health.model_version} · Uptime {Math.round(health.uptime_seconds)}s</span>}
        {secsAgo !== null && <span style={{ color: '#334155', fontSize: '0.75rem' }}>· updated {secsAgo}s ago</span>}
        <button onClick={fetchAll} style={{
          marginLeft: 'auto', background: 'transparent', border: '1px solid #1e293b', color: '#94a3b8',
          borderRadius: 6, padding: '4px 12px', fontSize: '0.75rem', cursor: 'pointer'
        }}>
          ↻ Refresh now
        </button>
        <a href="http://localhost:8021" target="_blank" rel="noopener noreferrer" style={{
          background: 'transparent', border: '1px solid #1e293b', color: '#60a5fa',
          borderRadius: 6, padding: '4px 12px', fontSize: '0.75rem', textDecoration: 'none'
        }}>PayStream feed ↗</a>
        <a href="http://localhost:5050" target="_blank" rel="noopener noreferrer" style={{
          background: 'transparent', border: '1px solid #1e293b', color: '#a78bfa',
          borderRadius: 6, padding: '4px 12px', fontSize: '0.75rem', textDecoration: 'none'
        }}>MLflow registry ↗</a>
        <a href="http://localhost:3100/d/payguard-fraud-api" target="_blank" rel="noopener noreferrer" style={{
          background: 'transparent', border: '1px solid #1e293b', color: '#f59e0b',
          borderRadius: 6, padding: '4px 12px', fontSize: '0.75rem', textDecoration: 'none'
        }}>Grafana dashboard ↗</a>
        <a href="http://localhost:9191/targets" target="_blank" rel="noopener noreferrer" style={{
          background: 'transparent', border: '1px solid #1e293b', color: '#e6522c',
          borderRadius: 6, padding: '4px 12px', fontSize: '0.75rem', textDecoration: 'none'
        }}>Prometheus targets ↗</a>
      </div>

      {/* KPI Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '1rem', marginBottom: '1.5rem' }}>
        <StatCard label="Transactions Scored" value={totalScored} color="#60a5fa" sub="since last restart" />
        <StatCard label="Fraud Rate" value={fraudRate} color="#ef4444" sub={stats ? `${stats.fraud_flagged} flagged` : ''} />
        <StatCard label="Avg Inference" value={avgLatency} color="#a78bfa" sub="sub-millisecond target" />
        <StatCard label="Model Version" value={`v${modelVersion}`} color="#34d399" sub={modelInfo ? `${modelInfo.model_name}` : 'XGBoost'} />
      </div>

      {/* Model Info + Fraud Rate Trend */}
      {modelInfo && (
        <Card style={{ marginBottom: '1.5rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1.5rem', flexWrap: 'wrap' }}>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '1.5rem', flex: 1, minWidth: 260 }}>
              <div>
                <div style={{ fontSize: '0.75rem', color: '#475569', marginBottom: 4, textTransform: 'uppercase', letterSpacing: 1 }}>Model Info · Name</div>
                <div style={{ color: '#e2e8f0', fontWeight: 500 }}>{modelInfo.model_name}</div>
              </div>
              <div>
                <div style={{ fontSize: '0.75rem', color: '#475569', marginBottom: 4, textTransform: 'uppercase', letterSpacing: 1 }}>Stage</div>
                <div style={{ color: '#a78bfa', fontWeight: 500 }}>{modelInfo.model_stage}</div>
              </div>
              <div>
                <div style={{ fontSize: '0.75rem', color: '#475569', marginBottom: 4, textTransform: 'uppercase', letterSpacing: 1 }}>Features</div>
                <div style={{ color: '#e2e8f0', fontWeight: 500 }}>{modelInfo.feature_cols?.length || 16} columns</div>
              </div>
            </div>
            <div>
              <div style={{ fontSize: '0.75rem', color: '#475569', marginBottom: 4, textTransform: 'uppercase', letterSpacing: 1 }}>Fraud Rate Trend (live)</div>
              <Sparkline points={fraudRateHistory} />
            </div>
          </div>
        </Card>
      )}

      {/* Recent Predictions */}
      {stats?.recent_predictions?.length > 0 && (
        <Card>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: '1rem' }}>
            <div style={{ fontSize: '0.875rem', fontWeight: 600, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: 1 }}>
              Recent Predictions
            </div>
            <div className="pg-live-dot" style={{ width: 6, height: 6, borderRadius: '50%', background: '#22c55e' }} />
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid #1e293b' }}>
                  {['Transaction ID', 'Amount', 'Type', 'Fraud Prob', 'Result', 'Latency'].map(h => (
                    <th key={h} style={{ padding: '8px 12px', color: '#475569', textAlign: 'left', fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.5 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {stats.recent_predictions.slice(0, 10).map((p, i) => (
                  <tr key={p.transaction_id || i}
                    className={newRowIds.has(p.transaction_id) ? 'pg-new-row' : ''}
                    style={{ borderBottom: '1px solid #0f172a' }}>
                    <td style={{ padding: '8px 12px', color: '#64748b', fontFamily: 'monospace' }}>{p.transaction_id?.slice(0, 12)}…</td>
                    <td style={{ padding: '8px 12px', color: '#e2e8f0' }}>${p.amount?.toFixed(2)}</td>
                    <td style={{ padding: '8px 12px', color: '#60a5fa' }}>{p.transaction_type}</td>
                    <td style={{ padding: '8px 12px' }}>
                      <div style={{ width: 80, height: 6, background: '#1e293b', borderRadius: 3 }}>
                        <div style={{ width: `${(p.fraud_probability * 100).toFixed(0)}%`, height: '100%', background: p.fraud_probability > 0.6 ? '#ef4444' : p.fraud_probability > 0.2 ? '#f59e0b' : '#22c55e', borderRadius: 3 }} />
                      </div>
                      <span style={{ color: '#94a3b8', fontSize: '0.75rem' }}>{(p.fraud_probability * 100).toFixed(1)}%</span>
                    </td>
                    <td style={{ padding: '8px 12px' }}>
                      <span style={{
                        padding: '2px 8px', borderRadius: 4, fontSize: '0.75rem', fontWeight: 600,
                        background: p.is_fraud ? '#7f1d1d' : '#14532d',
                        color: p.is_fraud ? '#fca5a5' : '#86efac'
                      }}>
                        {p.is_fraud ? 'FRAUD' : 'SAFE'}
                      </span>
                    </td>
                    <td style={{ padding: '8px 12px', color: '#475569', fontFamily: 'monospace' }}>{p.inference_time_ms?.toFixed(2)}ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {loading && !error && (
        <div style={{ textAlign: 'center', color: '#475569', padding: '3rem' }}>Loading...</div>
      )}
    </div>
  )
}
