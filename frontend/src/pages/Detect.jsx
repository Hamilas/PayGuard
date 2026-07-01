import { useState } from 'react'

const API = '/api'
const TXN_TYPES = ['TRANSFER', 'CASH_OUT', 'PAYMENT', 'DEBIT', 'CASH_IN']

function getRiskLabel(prob) {
  if (prob < 0.2) return { label: 'Safe', color: '#22c55e', bg: '#14532d' }
  if (prob < 0.6) return { label: 'Suspicious', color: '#f59e0b', bg: '#451a03' }
  return { label: 'High Risk', color: '#ef4444', bg: '#7f1d1d' }
}

const EXAMPLES = {
  safe: {
    label: 'Safe Payment',
    color: '#22c55e',
    values: { amount: '150.00', transaction_type: 'PAYMENT', oldbalanceOrg: '5000', newbalanceOrig: '4850', oldbalanceDest: '1000', newbalanceDest: '1150' }
  },
  suspicious: {
    label: 'Suspicious Transfer',
    color: '#f59e0b',
    values: { amount: '4200.00', transaction_type: 'TRANSFER', oldbalanceOrg: '4300', newbalanceOrig: '100', oldbalanceDest: '0', newbalanceDest: '4200' }
  },
  fraud: {
    label: 'High-Risk Cash-Out',
    color: '#ef4444',
    values: { amount: '9800.00', transaction_type: 'CASH_OUT', oldbalanceOrg: '9800', newbalanceOrig: '0', oldbalanceDest: '0', newbalanceDest: '0' }
  }
}

export default function Detect() {
  const [form, setForm] = useState({
    transaction_id: `txn-${Date.now()}`,
    amount: '',
    oldbalanceOrg: '0',
    newbalanceOrig: '0',
    oldbalanceDest: '0',
    newbalanceDest: '0',
    transaction_type: 'TRANSFER',
    step: '1',
    threshold: '0.5'
  })
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [history, setHistory] = useState([])

  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const loadExample = (key) => {
    setForm(f => ({ ...f, ...EXAMPLES[key].values, transaction_id: `txn-${Date.now()}` }))
    setResult(null)
    setError(null)
  }

  const submit = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const payload = {
        transaction_id: form.transaction_id,
        amount: parseFloat(form.amount),
        oldbalanceOrg: parseFloat(form.oldbalanceOrg) || 0,
        newbalanceOrig: parseFloat(form.newbalanceOrig) || 0,
        oldbalanceDest: parseFloat(form.oldbalanceDest) || 0,
        newbalanceDest: parseFloat(form.newbalanceDest) || 0,
        transaction_type: form.transaction_type,
        step: parseInt(form.step) || 1,
        threshold: parseFloat(form.threshold) || 0.5
      }
      const res = await fetch(`${API}/predict`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || `HTTP ${res.status}`)
      }
      const data = await res.json()
      setResult(data)
      setHistory(h => [{ ...data, submittedAt: Date.now() }, ...h].slice(0, 8))
      set('transaction_id', `txn-${Date.now()}`)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const risk = result ? getRiskLabel(result.fraud_probability) : null
  const probPct = result ? Math.round(result.fraud_probability * 100) : 0

  const inputStyle = {
    width: '100%',
    padding: '8px 12px',
    background: '#0a0f1a',
    border: '1px solid #1e293b',
    borderRadius: 6,
    color: '#e2e8f0',
    fontSize: '0.875rem',
    outline: 'none'
  }

  const labelStyle = {
    fontSize: '0.75rem',
    color: '#64748b',
    textTransform: 'uppercase',
    letterSpacing: 0.8,
    marginBottom: 6,
    display: 'block'
  }

  return (
    <div style={{ maxWidth: 760, margin: '0 auto' }}>
      <div style={{ marginBottom: '1.5rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#f1f5f9' }}>Detect Fraud</h1>
        <p style={{ color: '#64748b', marginTop: 4 }}>Submit a transaction to get an instant fraud probability score</p>
      </div>

      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem', flexWrap: 'wrap' }}>
        <span style={{ color: '#475569', fontSize: '0.75rem', alignSelf: 'center', textTransform: 'uppercase', letterSpacing: 0.8 }}>Quick fill:</span>
        {Object.entries(EXAMPLES).map(([key, ex]) => (
          <button key={key} type="button" onClick={() => loadExample(key)} style={{
            background: 'transparent', border: `1px solid ${ex.color}55`, color: ex.color,
            borderRadius: 6, padding: '4px 12px', fontSize: '0.75rem', cursor: 'pointer', fontWeight: 500
          }}>
            {ex.label}
          </button>
        ))}
      </div>

      <div style={{ background: '#0d1424', border: '1px solid #1e293b', borderRadius: 12, padding: '1.5rem', marginBottom: '1.5rem' }}>
        <form onSubmit={submit}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginBottom: '1rem' }}>
            <div>
              <label style={labelStyle}>Transaction ID</label>
              <input style={inputStyle} value={form.transaction_id} onChange={e => set('transaction_id', e.target.value)} required />
            </div>
            <div>
              <label style={labelStyle}>Amount ($) *</label>
              <input style={inputStyle} type="number" step="0.01" min="0.01" placeholder="e.g. 5000.00" value={form.amount} onChange={e => set('amount', e.target.value)} required />
            </div>
            <div>
              <label style={labelStyle}>Transaction Type</label>
              <select style={inputStyle} value={form.transaction_type} onChange={e => set('transaction_type', e.target.value)}>
                {TXN_TYPES.map(t => <option key={t}>{t}</option>)}
              </select>
            </div>
            <div>
              <label style={labelStyle}>Step (hour in sim)</label>
              <input style={inputStyle} type="number" min="0" value={form.step} onChange={e => set('step', e.target.value)} />
            </div>
            <div>
              <label style={labelStyle}>Origin Old Balance</label>
              <input style={inputStyle} type="number" step="0.01" min="0" value={form.oldbalanceOrg} onChange={e => set('oldbalanceOrg', e.target.value)} />
            </div>
            <div>
              <label style={labelStyle}>Origin New Balance</label>
              <input style={inputStyle} type="number" step="0.01" min="0" value={form.newbalanceOrig} onChange={e => set('newbalanceOrig', e.target.value)} />
            </div>
            <div>
              <label style={labelStyle}>Destination Old Balance</label>
              <input style={inputStyle} type="number" step="0.01" min="0" value={form.oldbalanceDest} onChange={e => set('oldbalanceDest', e.target.value)} />
            </div>
            <div>
              <label style={labelStyle}>Destination New Balance</label>
              <input style={inputStyle} type="number" step="0.01" min="0" value={form.newbalanceDest} onChange={e => set('newbalanceDest', e.target.value)} />
            </div>
          </div>

          <div style={{ marginBottom: '1.5rem' }}>
            <label style={labelStyle}>Threshold: {parseFloat(form.threshold).toFixed(2)}</label>
            <input type="range" min="0" max="1" step="0.05" value={form.threshold} onChange={e => set('threshold', e.target.value)}
              style={{ width: '100%', accentColor: '#ef4444' }} />
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.75rem', color: '#475569', marginTop: 4 }}>
              <span>Lenient (0.0)</span><span>Strict (1.0)</span>
            </div>
          </div>

          <button type="submit" disabled={loading} style={{
            width: '100%', padding: '12px', borderRadius: 8, border: 'none', cursor: loading ? 'not-allowed' : 'pointer',
            background: loading ? '#374151' : '#ef4444', color: '#fff', fontWeight: 700, fontSize: '1rem', transition: 'background 0.15s',
            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8
          }}>
            {loading && <span className="pg-spinner" style={{ width: 14, height: 14, border: '2px solid #94a3b8', borderTopColor: 'transparent', borderRadius: '50%', display: 'inline-block' }} />}
            {loading ? 'Scoring...' : 'Score Transaction'}
          </button>
        </form>
      </div>

      {error && (
        <div style={{ background: '#1a0a0a', border: '1px solid #7f1d1d', borderRadius: 8, padding: '1rem', color: '#fca5a5', marginBottom: '1rem' }}>
          Error: {error}
        </div>
      )}

      {result && risk && (
        <div style={{ background: '#0d1424', border: `1px solid ${risk.color}33`, borderRadius: 12, padding: '1.5rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem' }}>
            <div>
              <div style={{ fontSize: '0.75rem', color: '#64748b', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 4 }}>Fraud Probability</div>
              <div style={{ fontSize: '3rem', fontWeight: 800, color: risk.color }}>{probPct}%</div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <span style={{ padding: '8px 20px', borderRadius: 8, fontWeight: 700, fontSize: '1.1rem', background: risk.bg, color: risk.color }}>
                {risk.label}
              </span>
              <div style={{ marginTop: 8, fontSize: '0.8rem', color: '#475569' }}>
                {result.is_fraud ? 'Transaction BLOCKED' : 'Transaction APPROVED'}
              </div>
            </div>
          </div>

          {/* Probability bar */}
          <div style={{ marginBottom: '1.5rem' }}>
            <div style={{ height: 12, background: '#1e293b', borderRadius: 6, overflow: 'hidden' }}>
              <div style={{
                width: `${probPct}%`, height: '100%', borderRadius: 6, transition: 'width 0.5s ease',
                background: probPct > 60 ? '#ef4444' : probPct > 20 ? '#f59e0b' : '#22c55e'
              }} />
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 4, fontSize: '0.75rem', color: '#475569' }}>
              <span>Safe (&lt;20%)</span><span>Suspicious (20-60%)</span><span>High Risk (&gt;60%)</span>
            </div>
          </div>

          {/* Details */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '1rem', fontSize: '0.8rem' }}>
            <div style={{ background: '#0a0f1a', borderRadius: 8, padding: '0.75rem' }}>
              <div style={{ color: '#475569', marginBottom: 4 }}>Transaction ID</div>
              <div style={{ color: '#94a3b8', fontFamily: 'monospace', wordBreak: 'break-all' }}>{result.transaction_id}</div>
            </div>
            <div style={{ background: '#0a0f1a', borderRadius: 8, padding: '0.75rem' }}>
              <div style={{ color: '#475569', marginBottom: 4 }}>Model</div>
              <div style={{ color: '#94a3b8' }}>{result.model_name} v{result.model_version}</div>
            </div>
            <div style={{ background: '#0a0f1a', borderRadius: 8, padding: '0.75rem' }}>
              <div style={{ color: '#475569', marginBottom: 4 }}>Inference Time</div>
              <div style={{ color: '#a78bfa', fontWeight: 600 }}>{result.inference_time_ms.toFixed(3)} ms</div>
            </div>
          </div>
        </div>
      )}

      {history.length > 0 && (
        <div style={{ marginTop: '1.5rem', background: '#0d1424', border: '1px solid #1e293b', borderRadius: 12, padding: '1.5rem' }}>
          <div style={{ fontSize: '0.875rem', fontWeight: 600, color: '#94a3b8', marginBottom: '1rem', textTransform: 'uppercase', letterSpacing: 1 }}>
            This Session ({history.length})
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {history.map((h, i) => {
              const r = getRiskLabel(h.fraud_probability)
              return (
                <div key={h.transaction_id + i} className={i === 0 ? 'pg-new-row' : ''} style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                  padding: '6px 10px', borderRadius: 6, fontSize: '0.8rem', background: '#0a0f1a'
                }}>
                  <span style={{ color: '#64748b', fontFamily: 'monospace' }}>{h.transaction_id?.slice(0, 14)}…</span>
                  <span style={{ color: '#475569' }}>{new Date(h.submittedAt).toLocaleTimeString()}</span>
                  <span style={{ color: r.color, fontWeight: 600 }}>{Math.round(h.fraud_probability * 100)}% · {r.label}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
