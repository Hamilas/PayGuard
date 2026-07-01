export default function About() {
  const stack = [
    { name: 'XGBoost', role: 'Fraud detection model', color: '#f59e0b' },
    { name: 'MLflow', role: 'Model registry & experiment tracking', color: '#0194E2' },
    { name: 'LangGraph', role: '19-node autonomous SRE agents', color: '#6366f1' },
    { name: 'FastAPI', role: 'Real-time inference API', color: '#009688' },
    { name: 'Docker Compose', role: 'Local orchestration', color: '#2496ED' },
    { name: 'Prometheus', role: 'Scrapes fraud-api /metrics every 15s', color: '#e6522c' },
    { name: 'Grafana', role: 'Auto-provisioned PayGuard Fraud API dashboard', color: '#f46800' },
    { name: 'Redis', role: 'Feature cache & agent dedup', color: '#dc382d' },
    { name: 'React + Vite', role: 'This dashboard', color: '#61dafb' },
  ]

  const archNodes = [
    { label: 'PayStream :8021', sub: 'Transaction simulator, running', color: '#0194E2' },
    { label: 'Fraud API :8020', sub: 'FastAPI + XGBoost, running', color: '#ef4444' },
    { label: 'MLflow :5050', sub: 'Model registry, running', color: '#0194E2' },
    { label: 'Redis :6379', sub: 'Feature cache, running', color: '#dc382d' },
    { label: 'Prometheus :9191', sub: 'Metrics scraper, running', color: '#e6522c' },
    { label: 'Grafana :3100', sub: 'Live dashboard, running', color: '#f46800' },
    { label: 'LangGraph agents', sub: '19 SRE nodes, reference design, not deployed here', color: '#6366f1' },
  ]

  return (
    <div style={{ maxWidth: 800, margin: '0 auto' }}>
      <div style={{ marginBottom: '1.5rem' }}>
        <h1 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#f1f5f9' }}>About PayGuard</h1>
        <p style={{ color: '#64748b', marginTop: 4 }}>Architecture, stack, and author information</p>
      </div>

      {/* Description */}
      <div style={{ background: '#0d1424', border: '1px solid #1e293b', borderRadius: 12, padding: '1.5rem', marginBottom: '1.5rem' }}>
        <p style={{ color: '#94a3b8', lineHeight: 1.7 }}>
          PayGuard is a real-time payment fraud detection system: a synthetic payment stream
          sends transactions to a FastAPI service that engineers 16 risk features and scores
          them with an XGBoost classifier loaded live from an MLflow model registry.
          The design includes a 19-node LangGraph multi-LLM agent for autonomously investigating
          and remediating fraud-rate alerts (reference architecture, not deployed in this local
          compose), built for European fintechs and payment processors.
        </p>
      </div>

      {/* Architecture */}
      <div style={{ background: '#0d1424', border: '1px solid #1e293b', borderRadius: 12, padding: '1.5rem', marginBottom: '1.5rem' }}>
        <h2 style={{ fontSize: '1rem', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: 1, marginBottom: '1rem' }}>Architecture</h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.75rem' }}>
          {archNodes.map((n, i) => (
            <div key={i} style={{
              background: '#0a0f1a', border: `1px solid ${n.color}44`,
              borderRadius: 8, padding: '0.75rem 1rem', minWidth: 160
            }}>
              <div style={{ fontSize: '0.875rem', fontWeight: 600, color: n.color, marginBottom: 4 }}>{n.label}</div>
              <div style={{ fontSize: '0.75rem', color: '#475569' }}>{n.sub}</div>
            </div>
          ))}
        </div>
        <div style={{ marginTop: '1rem', padding: '0.75rem', background: '#0a0f1a', borderRadius: 8, fontFamily: 'monospace', fontSize: '0.8rem', color: '#475569' }}>
          PayStream → POST /predict → Fraud API → XGBoost (MLflow Staging)<br/>
          LangGraph SRE agents ↔ Prometheus alerts ↔ Fraud API
        </div>
      </div>

      {/* Stack Table */}
      <div style={{ background: '#0d1424', border: '1px solid #1e293b', borderRadius: 12, padding: '1.5rem', marginBottom: '1.5rem' }}>
        <h2 style={{ fontSize: '1rem', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: 1, marginBottom: '1rem' }}>Tech Stack</h2>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #1e293b' }}>
              <th style={{ padding: '8px 12px', color: '#475569', textAlign: 'left', fontWeight: 600 }}>Technology</th>
              <th style={{ padding: '8px 12px', color: '#475569', textAlign: 'left', fontWeight: 600 }}>Role</th>
            </tr>
          </thead>
          <tbody>
            {stack.map((s, i) => (
              <tr key={i} style={{ borderBottom: '1px solid #0f172a' }}>
                <td style={{ padding: '10px 12px' }}>
                  <span style={{ fontWeight: 600, color: s.color }}>{s.name}</span>
                </td>
                <td style={{ padding: '10px 12px', color: '#94a3b8' }}>{s.role}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Author */}
      <div style={{ background: '#0d1424', border: '1px solid #1e293b', borderRadius: 12, padding: '1.5rem' }}>
        <h2 style={{ fontSize: '1rem', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: 1, marginBottom: '1rem' }}>Author</h2>
        <div style={{ display: 'flex', gap: '1rem', alignItems: 'flex-start' }}>
          <div style={{ width: 48, height: 48, background: '#ef4444', borderRadius: 12, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '1.2rem', fontWeight: 700, color: '#fff', flexShrink: 0 }}>RL</div>
          <div>
            <div style={{ fontWeight: 700, color: '#f1f5f9', fontSize: '1rem' }}>Rayen Lassoued</div>
            <div style={{ color: '#64748b', fontSize: '0.875rem', marginTop: 4 }}>AI/ML Engineer · Final-year CS student · Bonn, Germany</div>
            <div style={{ display: 'flex', gap: '1rem', marginTop: 12 }}>
              <a href="https://github.com/Hamilas" target="_blank" rel="noopener noreferrer"
                style={{ color: '#60a5fa', textDecoration: 'none', fontSize: '0.875rem' }}>
                github.com/Hamilas
              </a>
              <a href="https://www.linkedin.com/in/lassoued-rayen/" target="_blank" rel="noopener noreferrer"
                style={{ color: '#60a5fa', textDecoration: 'none', fontSize: '0.875rem' }}>
                https://www.linkedin.com/in/lassoued-rayen/
              </a>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
