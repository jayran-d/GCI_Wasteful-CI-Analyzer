import { useState, useRef, useEffect } from 'react'
import { Bar, Doughnut } from 'react-chartjs-2'
import {
  Chart as ChartJS, CategoryScale, LinearScale, BarElement, Title, Tooltip,
  ArcElement, Legend
} from 'chart.js'

ChartJS.register(CategoryScale, LinearScale, BarElement, Title, Tooltip, ArcElement, Legend)

const API = 'http://localhost:5001'
const STEPS = ['Connect', 'Fetch runs', 'Overview', 'Analyze', 'Report']
const fmt3 = (v) => {
  if (v === 0) return '0'
  const abs = Math.abs(v)
  if (abs >= 100) return v.toFixed(1)
  if (abs >= 10) return v.toFixed(2)
  if (abs >= 1) return v.toFixed(3)
  if (abs >= 0.01) return v.toFixed(4)
  return v.toPrecision(3)
}

const ANALYZER_META = {
  flakiness:              { color: '#a78bfa', glow: 'rgba(167,139,250,0.3)' },
  zombie_scheduled:       { color: '#f07a6e', glow: 'rgba(240,122,110,0.3)' },
  external_deps:          { color: '#5b9cf5', glow: 'rgba(91,156,245,0.3)' },
  inefficient_triggers:   { color: '#f5b731', glow: 'rgba(245,183,49,0.3)' },
  workflow_dependencies:  { color: '#2dd4a8', glow: 'rgba(45,212,168,0.3)' },
}

function extractFlaggedRunIds(result) {
  const flaggedRunIds = []
  if (!result) return flaggedRunIds

  if (Array.isArray(result.flagged_run_ids) && result.flagged_run_ids.length > 0) {
    flaggedRunIds.push(...result.flagged_run_ids)
    return flaggedRunIds
  }

  const seen = new Set()
  const addId = (rid) => {
    if (rid && typeof rid === 'number' && !seen.has(rid)) {
      seen.add(rid)
      flaggedRunIds.push(rid)
    }
  }
  const scan = (obj, key) => {
    if (!obj || typeof obj !== 'object') return
    if (Array.isArray(obj)) {
      if (key && (key.endsWith('_ids') || key === 'run_ids')) {
        for (const item of obj) {
          if (typeof item === 'number') addId(item)
        }
      } else {
        for (const item of obj) scan(item)
      }
      return
    }
    addId(obj.run_id || obj.child_run_id)
    for (const [k, v] of Object.entries(obj)) {
      if (['summary', 'energy_waste', 'recommendations'].includes(k)) continue
      if (typeof v === 'object' && v !== null) scan(v, k)
    }
  }

  scan(result)
  return flaggedRunIds
}

export default function App() {
  const [phase, setPhase] = useState('input')
  const [step, setStep] = useState(0)
  const [error, setError] = useState(null)
  const [repo, setRepo] = useState('')
  const [startDate, setStartDate] = useState(() => {
    const d = new Date(); d.setDate(d.getDate() - 90); return d.toISOString().slice(0, 10)
  })
  const [endDate, setEndDate] = useState(() => new Date().toISOString().slice(0, 10))
  const [token, setToken] = useState('')
  const [geminiKey, setGeminiKey] = useState('')
  const [deepScan, setDeepScan] = useState(true)
  const [repoLabel, setRepoLabel] = useState('')
  const [rateInfo, setRateInfo] = useState(null)
  const [fetchProgress, setFetchProgress] = useState({ fetched: 0, total: 0, page: 0 })
  const [fetchDone, setFetchDone] = useState(false)
  const [workflows, setWorkflows] = useState([])
  const [eventBreakdown, setEventBreakdown] = useState({})
  const [analyzers, setAnalyzers] = useState({})
  const [analyzerOrder, setAnalyzerOrder] = useState([])
  const [grandTotal, setGrandTotal] = useState(null)
  const [allRuns, setAllRuns] = useState([])

  const handleEvent = (msg) => {
    const ev = msg.event
    if (ev === 'error') { setError(msg.message); return }
    if (ev === 'connected') {
      setStep(1); setRepoLabel(`${msg.owner}/${msg.repo}`)
      if (msg.rate_limit?.remaining) setRateInfo(msg.rate_limit)
      if (msg.warnings?.length) msg.warnings.forEach(w => setError(prev => prev ? prev + '\n' + w : w))
    }
    if (ev === 'warning') setError(prev => prev ? prev + '\n' + msg.message : msg.message)
    if (ev === 'runs_page') {
      setFetchProgress({ fetched: msg.fetched_so_far, total: msg.total_available, page: msg.page })
      if (msg.runs) setAllRuns(prev => [...prev, ...msg.runs])
      if (msg.rate_remaining != null) setRateInfo(prev => prev ? { ...prev, remaining: msg.rate_remaining } : { remaining: msg.rate_remaining, limit: '?' })
    }
    if (ev === 'runs_complete') { setStep(2); setFetchDone(true); setWorkflows(msg.workflows || []); setEventBreakdown(msg.event_breakdown || {}) }
    if (ev === 'analyzer_start') {
      setStep(3)
      setAnalyzerOrder(prev => prev.includes(msg.key) ? prev : [...prev, msg.key])
      setAnalyzers(prev => prev[msg.key] ? prev : {
        ...prev, [msg.key]: { status: 'running', title: msg.title, description: msg.description, logs: [], result: null }
      })
    }
    if (ev === 'analyzer_progress') {
      setAnalyzers(prev => {
        const a = prev[msg.key]; if (!a) return prev
        return { ...prev, [msg.key]: { ...a, logs: [...a.logs, msg.msg || msg.message || JSON.stringify(msg)] } }
      })
    }
    if (ev === 'analyzer_complete') {
      setAnalyzers(prev => {
        const a = prev[msg.key]; if (!a) return prev
        return { ...prev, [msg.key]: { ...a, status: 'complete', result: msg.result } }
      })
    }
    if (ev === 'complete') {
      setStep(4)
      setGrandTotal({
        categorized: msg.grand_total,
        all_failures: msg.all_failures,
        all_successes: msg.all_successes,
        total_runs: msg.total_runs,
        total_failed: msg.total_failed,
        failure_rate: msg.failure_rate,
        impact: msg.impact,
        carbon_intensity: msg.carbon_intensity_g_per_kwh,
        top_failing_workflows: msg.top_failing_workflows,
      })
    }
  }

  const startAnalysis = async () => {
    if (!repo.trim()) return
    setPhase('analysis'); setStep(0); setError(null)
    setFetchProgress({ fetched: 0, total: 0, page: 0 }); setFetchDone(false)
    setWorkflows([]); setAnalyzers({}); setAnalyzerOrder([]); setGrandTotal(null)
    setAllRuns([])
    try {
      const resp = await fetch(API + '/api/analyze/stream', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_url: repo, start_date: startDate || undefined, end_date: endDate || undefined, github_token: token || undefined, deep_scan: deepScan }),
      })
      const reader = resp.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
      while (true) {
        const { done, value } = await reader.read(); if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n'); buffer = lines.pop()
        for (const line of lines) {
          if (line.trim().startsWith('data: ')) { try { handleEvent(JSON.parse(line.trim().slice(6))) } catch {} }
        }
      }
    } catch (err) { setError('Connection failed: ' + err.message) }
  }

  const exportJSON = () => {
    const blob = new Blob([JSON.stringify({ analyzers, grandTotal }, null, 2)], { type: 'application/json' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'gci-report.json'; a.click()
  }

  if (phase === 'input') {
    return (
      <div className="container">
        <header className="header">
          <div className="logo-mark">
            <span className="logo-leaf">🌿</span>
            <h1><span className="green">G</span><span className="white">C</span><span className="green">I</span></h1>
          </div>
          <p className="tagline">Green Continuous Integration Analyzer</p>
          <p className="subtitle">Uncover hidden energy waste in your GitHub Actions pipelines</p>
        </header>
        <div className="card form-card">
          <div className="form-row">
            <div className="form-group wide">
              <label>Repository</label>
              <input value={repo} onChange={e => setRepo(e.target.value)} placeholder="https://github.com/owner/repo" />
            </div>
          </div>
          <div className="form-row">
            <div className="form-group"><label>Start date</label><input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} /></div>
            <div className="form-group"><label>End date</label><input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} /></div>
          </div>
          <div className="form-row">
            <div className="form-group"><label>GitHub token</label><input type="password" value={token} onChange={e => setToken(e.target.value)} placeholder="ghp_..." /></div>
            <div className="form-group"><label>Gemini API key <span className="optional">(for AI diagnosis)</span></label><input type="password" value={geminiKey} onChange={e => setGeminiKey(e.target.value)} placeholder="AIza..." /></div>
          </div>
          <div className="form-footer">
            <button className="btn-go" onClick={startAnalysis}><span className="btn-icon">⚡</span> Analyze</button>
          </div>
        </div>
        <div className="features-row">
          {[
            { label: 'Flaky Tests' }, { label: 'Zombie Crons' },
            { label: 'Dep Failures' }, { label: 'Trigger Waste' },
            { label: 'Cascade Fails' },
          ].map((f, i) => (
            <div key={i} className="feature-chip" style={{ animationDelay: `${i * 0.1}s` }}>
              <span>{f.icon}</span> {f.label}
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="container">
      <div className="top-bar">
        <button className="btn-ghost" onClick={() => setPhase('input')}>← Back</button>
        <span className="repo-label">{repoLabel}</span>
        {rateInfo && <span className="rate-label">API: {rateInfo.remaining}/{rateInfo.limit}</span>}
      </div>
      <Pipeline step={step} />
      {error && <div className="warning-box">{error.split('\n').map((line, i) => <div key={i}>{line}</div>)}</div>}
      {step >= 1 && <FetchProgress progress={fetchProgress} done={fetchDone} />}
      {step >= 2 && <WorkflowTable workflows={workflows} events={eventBreakdown} />}
      {analyzerOrder.map(key => (
        <AnalyzerCard key={key} id={key} data={analyzers[key]} repo={repo} token={token} geminiKey={geminiKey}
          repoLabel={repoLabel} allRuns={allRuns} />
      ))}
      {grandTotal && <Report grandTotal={grandTotal} analyzers={analyzers} order={analyzerOrder} />}
      {grandTotal && (
        <div className="export-bar">
          <button className="btn-ghost" onClick={exportJSON}>📥 Export JSON</button>
        </div>
      )}
    </div>
  )
}

/* ──── Sub-components ──── */

function Pipeline({ step }) {
  return (
    <div className="pipeline">
      {STEPS.map((label, i) => (
        <div key={i} style={{ display: 'flex', alignItems: 'center' }}>
          <div className={`pipe-step ${i < step ? 'done' : i === step ? 'active' : ''}`}>
            <span className="dot" />{label}
          </div>
          {i < STEPS.length - 1 && <div className="pipe-sep" />}
        </div>
      ))}
    </div>
  )
}

function FetchProgress({ progress, done }) {
  const pct = progress.total > 0
    ? Math.min(100, Math.round(progress.fetched / Math.min(progress.total, 1000) * 100)) : 0
  return (
    <div className="card section fade-in">
      <div className="section-label"> Fetching workflow runs</div>
      <div className="fetch-stats">
        <span className="fetch-count">{progress.fetched}</span>
        <span className="fetch-total">
          {done ? `✓ ${progress.fetched} runs fetched` : `Page ${progress.page} · ${progress.total.toLocaleString()} available`}
        </span>
      </div>
      <div className="progress-track"><div className="progress-fill" style={{ width: (done ? 100 : pct) + '%' }} /></div>
    </div>
  )
}

function WorkflowTable({ workflows, events }) {
  return (
    <div className="section fade-in">
      <div className="section-label"> Workflow overview</div>
      <div className="event-chips">
        {Object.entries(events).map(([k, v]) => <span key={k} className="chip">{k}: {v}</span>)}
      </div>
      <div className="card table-wrap">
        <table className="wf-table">
          <thead><tr><th>Workflow</th><th style={{ textAlign: 'center' }}>Runs</th><th>Pass / Fail</th><th>✓</th><th>✗</th></tr></thead>
          <tbody>
            {workflows.slice(0, 15).map((w, i) => {
              const okP = w.total > 0 ? (w.success / w.total * 100) : 0
              const failP = w.total > 0 ? (w.failure / w.total * 100) : 0
              return (
                <tr key={i}>
                  <td><span className="mono">{w.name}</span></td>
                  <td style={{ textAlign: 'center' }}>{w.total}</td>
                  <td><div className="wf-bar"><div className="wf-bar-ok" style={{ width: okP + '%' }} /><div className="wf-bar-fail" style={{ width: failP + '%' }} /><div className="wf-bar-other" style={{ width: (100 - okP - failP) + '%' }} /></div></td>
                  <td><span className="badge-ok">{w.success}</span></td>
                  <td><span className="badge-fail">{w.failure}</span></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function AnalyzerCard({ id, data, repo, token, geminiKey, repoLabel, allRuns }) {
  const [open, setOpen] = useState(true)
  const logRef = useRef(null)
  const meta = ANALYZER_META[id] || { color: '#6b7a8d', glow: 'rgba(107,122,141,0.3)' }
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight }, [data.logs])

  const s = data.result?.summary || {}
  const e = data.result?.energy_waste || {}
  const pct = s.waste_percentage ?? s.flakiness_rate_of_failures ?? 0
  const severity = pct > 15 ? 'bad' : pct > 3 ? 'warn' : 'ok'

  const flaggedRunIds = extractFlaggedRunIds(data.result)

  const linkedRunIdSet = new Set(flaggedRunIds)

  // Build analyzer context to pass to AI diagnosis
  const analyzerContext = data.result && !data.result.error ? {
    key: id,
    title: data.title,
    description: data.description,
    summary: data.result?.summary,
    recommendations: data.result?.recommendations,
    energy_waste: data.result?.energy_waste,
  } : null

  return (
    <div className="card analyzer-card fade-in" style={{ '--az-color': meta.color, '--az-glow': meta.glow }}>
      <div className="az-header" onClick={() => setOpen(!open)}>
        <div className={`az-dot ${data.status === 'running' ? 'running' : severity}`} />
        <span className="az-title">{meta.icon} {data.title}</span>
        {data.status === 'complete' && <span className={`az-badge ${severity}`}>{fmt3(pct)}% waste</span>}
        <span className="az-chevron">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="az-body">
          {data.logs.length > 0 && (
            <div className="log-box" ref={logRef}>
              {data.logs.map((msg, i) => <LogLine key={i} text={msg} />)}
              {data.status === 'running' && <div className="log-line log-cursor">█</div>}
            </div>
          )}
          {data.status === 'running' && data.logs.length === 0 && (
            <div className="running-msg"><span className="spinner" /> Running analysis...</div>
          )}
          {data.result && !data.result.error && (
            <>
              <div className="metrics-grid">
                {Object.entries(s).filter(([k]) => !['detection_mode','scope','event_scope'].includes(k)).map(([k, v]) => (
                  <div key={k} className="metric">
                    <div className="metric-val">{typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString() : fmt3(v)) : String(v)}</div>
                    <div className="metric-lbl">{k.replace(/_/g, ' ')}</div>
                  </div>
                ))}
                {e.total_energy_wh > 0 && <>
                  <div className="metric highlight"><div className="metric-val">{fmt3(e.total_energy_wh)} Wh</div><div className="metric-lbl">energy wasted</div></div>
                  <div className="metric highlight"><div className="metric-val">{fmt3(e.total_carbon_grams_co2)}g</div><div className="metric-lbl">CO₂</div></div>
                  <div className="metric highlight"><div className="metric-val">${fmt3(e.total_cost_usd)}</div><div className="metric-lbl">est. cost</div></div>
                </>}
              </div>
              {geminiKey && flaggedRunIds.length > 0 && (
                <AIDiagnosePanel
                  repo={repo}
                  token={token}
                  geminiKey={geminiKey}
                  runIds={flaggedRunIds}
                  analyzerContext={analyzerContext}
                />
              )}
              {data.result.recommendations?.length > 0 && (
                <div className="recs">
                  {data.result.recommendations.map((r, i) => {
                    if (r.includes('\n')) {
                      const [text, ...code] = r.split('\n')
                      return <div key={i} className="rec-item">→ {text}<pre className="rec-code">{code.join('\n')}</pre></div>
                    }
                    return <div key={i} className="rec-item">→ {r}</div>
                  })}
                </div>
              )}
              {allRuns.length > 0 && (
                <DebugRunsPanel
                  allRuns={allRuns}
                  linkedRunIds={linkedRunIdSet}
                  repoLabel={repoLabel}
                />
              )}
              <DetailTables result={data.result} />
            </>
          )}
        </div>
      )}
    </div>
  )
}

/* ──── Debug Runs Panel ──── */

function DebugRunsPanel({ allRuns, linkedRunIds, repoLabel }) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('linked')

  const failedCount = allRuns.filter(r => r.conclusion === 'failure').length
  const successCount = allRuns.filter(r => r.conclusion === 'success').length
  const unflaggedCount = allRuns.filter(r => !linkedRunIds.has(r.id)).length

  const filtered = allRuns.filter(r => {
    if (filter === 'linked') return linkedRunIds.has(r.id)
    if (filter === 'unflagged') return !linkedRunIds.has(r.id)
    if (filter === 'failed') return r.conclusion === 'failure'
    if (filter === 'success') return r.conclusion === 'success'
    return true
  })

  return (
    <div className="debug-runs-panel">
      <div className="debug-runs-toggle" onClick={() => setOpen(!open)}>
        <span className="debug-runs-icon"></span>
        <span className="debug-runs-label">
          Flagged runs: {linkedRunIds.size}
          <span className="debug-linked-count"> · {allRuns.length} total fetched</span>
        </span>
        <span className="debug-runs-chevron">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="debug-runs-body">
          <div className="debug-filter-bar">
            {[
              ['linked', `Flagged (${linkedRunIds.size})`],
               ['unflagged', `Unflagged (${unflaggedCount})`],
              ['all', `All (${allRuns.length})`],
              ['failed', `Failed (${failedCount})`],
              ['success', `Success (${successCount})`],
            ].map(([key, label]) => (
              <button
                key={key}
                className={`debug-filter-btn ${filter === key ? 'active' : ''}`}
                onClick={() => setFilter(key)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="debug-runs-list">
            <div className="debug-runs-header">
              <span className="debug-col-status">Status</span>
              <span className="debug-col-id">Run ID</span>
              <span className="debug-col-name">Workflow</span>
              <span className="debug-col-commit">Commit</span>
              <span className="debug-col-branch">Branch</span>
              <span className="debug-col-link">Link</span>
            </div>
            {filtered.map((r) => (
              <div key={r.id} className={`debug-run-row ${linkedRunIds.has(r.id) ? 'debug-row-linked' : ''}`}>
                <span className={`debug-run-status ${r.conclusion === 'success' ? 'status-ok' : r.conclusion === 'failure' ? 'status-fail' : 'status-other'}`}>
                  {r.conclusion === 'success' ? '✓' : r.conclusion === 'failure' ? '✗' : '●'}
                </span>
                <span className="debug-run-id">#{r.id}</span>
                <span className="debug-run-name" title={r.name}>{r.name}</span>
                <span className="debug-run-commit" title={r.commit_message || r.head_sha || ''}>
                  {r.head_sha ? (
                    <a
                      className="debug-sha-link"
                      href={`https://github.com/${repoLabel}/commit/${r.head_sha}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={r.commit_message || r.head_sha}
                    >
                      <span className="debug-sha">{r.head_sha.slice(0, 7)}</span>
                      {r.commit_message && (
                        <span className="debug-commit-msg">
                          {r.commit_message.length > 40 ? r.commit_message.slice(0, 37) + '…' : r.commit_message}
                        </span>
                      )}
                    </a>
                  ) : '—'}
                </span>
                <span className="debug-run-branch" title={r.head_branch}>{r.head_branch || '—'}</span>
                <a
                  className="debug-run-link"
                  href={r.html_url || `https://github.com/${repoLabel}/actions/runs/${r.id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  ↗
                </a>
              </div>
            ))}
            {filtered.length === 0 && (
              <div className="debug-empty">No runs match this filter</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/* ──── AI Diagnose Panel ──── */

function AIDiagnosePanel({ repo, token, geminiKey, runIds, analyzerContext }) {
  const [diagnosing, setDiagnosing] = useState(false)
  const [diagnosis, setDiagnosis] = useState(null)
  const [diagError, setDiagError] = useState(null)
  const [selectedRun, setSelectedRun] = useState(runIds[0])

  const diagnose = async () => {
    setDiagnosing(true); setDiagnosis(null); setDiagError(null)
    try {
      const resp = await fetch(API + '/api/diagnose', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          repo_url: repo,
          run_id: selectedRun,
          gemini_key: geminiKey,
          github_token: token || undefined,
          analyzer_context: analyzerContext || undefined,
        }),
      })
      const data = await resp.json()
      if (data.error) setDiagError(data.error)
      else setDiagnosis(data.diagnosis)
    } catch (e) { setDiagError('Request failed: ' + e.message) }
    setDiagnosing(false)
  }

  return (
    <div className="ai-panel">
      <div className="ai-panel-header">
        <span className="ai-label">AI Failure Diagnosis</span>
        <select className="ai-select" value={selectedRun} onChange={e => { setSelectedRun(Number(e.target.value)); setDiagnosis(null) }}>
          {runIds.map(id => <option key={id} value={id}>Run #{id}</option>)}
        </select>
        <button className="btn-ai" onClick={diagnose} disabled={diagnosing}>
          {diagnosing ? <><span className="spinner-sm" /> Analyzing...</> : ' Diagnose with Gemini'}
        </button>
      </div>
      {diagError && <div className="ai-error">{diagError}</div>}
      {diagnosis && (
        <div className="ai-result fade-in">
          <div className="ai-result-content" dangerouslySetInnerHTML={{ __html: markdownToHtml(diagnosis) }} />
        </div>
      )}
    </div>
  )
}

function markdownToHtml(md) {
  return md
    .replace(/```([\s\S]*?)```/g, '<pre class="ai-code">$1</pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\n/g, '<br/>')
}

function LogLine({ text }) {
  const trimmed = text.trimStart()
  const flag = trimmed.startsWith('→') || trimmed.startsWith('✓') || trimmed.startsWith('↳')
  const parts = text.split(/(https?:\/\/[^\s]+)/g)
  return (
    <div className={`log-line ${flag ? 'log-flag' : ''}`}>
      {parts.map((part, i) => part.startsWith('http')
        ? <a key={i} href={part} target="_blank" rel="noopener noreferrer" className="log-link">↗</a> : part
      )}
    </div>
  )
}

function DetailTables({ result }) {
  const [openKey, setOpenKey] = useState(null)
  const keys = [
    'top_offender_workflows','flaky_groups','zombies','early_death_transients',
    'temporal_clusters','third_party_action_failures','setup_step_failures',
    'doc_only_runs','redundant_concurrent_runs','workflows_missing_config',
    'short_failures','suspicious_cancellations','flaky_runs','zombie_workflows',
    'failure_streaks','deprecated_runners','deprecated_configs',
  ]
  return keys.map(dk => {
    let items = result[dk]
    if (items?.detail) items = items.detail
    if (items?.findings) items = items.findings
    if (!Array.isArray(items) || items.length === 0) return null
    const cols = Object.keys(items[0]).filter(k => !['labels','failed_jobs','log_preview','parent','parsed','log_patterns','failed_run_ids','run_ids'].includes(k)).slice(0, 5)
    const isOpen = openKey === dk
    return (
      <div key={dk}>
        <span className="detail-toggle" onClick={() => setOpenKey(isOpen ? null : dk)}>
          {isOpen ? '▾' : '▸'} {dk.replace(/_/g, ' ')} ({items.length})
        </span>
        {isOpen && (
          <table className="detail-table">
            <thead><tr>{cols.map(c => <th key={c}>{c.replace(/_/g, ' ')}</th>)}</tr></thead>
            <tbody>
              {items.slice(0, 10).map((row, i) => (
                <tr key={i}>{cols.map(c => {
                  let v = row[c]
                  if (Array.isArray(v)) v = v.join(', ')
                  if (typeof v === 'object' && v !== null) v = JSON.stringify(v)
                  if (typeof v === 'string' && v.length > 60) v = v.slice(0, 57) + '…'
                  return <td key={c}>{String(v ?? '')}</td>
                })}</tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    )
  })
}

/* ──── Report ──── */

function Report({ grandTotal, analyzers, order }) {
  const af = grandTotal.all_failures || {}
  const fr = grandTotal.failure_rate || 0
  const impact = grandTotal.impact || {}
  const topFailing = grandTotal.top_failing_workflows || []
  const [showImpact, setShowImpact] = useState(false)

  const labels = [], wasteCounts = [], energyVals = [], colors = []
  for (const key of order) {
    const a = analyzers[key]
    if (!a?.result || a.result.error) continue
    const s = a.result.summary || {}
    const ew = a.result.energy_waste || {}
    labels.push(a.title.length > 28 ? a.title.slice(0, 25) + '…' : a.title)
    colors.push(ANALYZER_META[key]?.color || '#6b7a8d')
    wasteCounts.push(s.flaky_failures || s.total_zombie_failed_runs || s.external_dep_failures || s.inefficient_run_count || s.flaky_runs_detected || 0)
    energyVals.push(ew.total_energy_wh || (ew.total_energy_kwh || 0) * 1000)
  }

  const chartOpts = {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false }, title: { display: true, color: '#c5cdd8', font: { family: 'Outfit', size: 13 } } },
    scales: {
      x: { ticks: { color: '#6b7a8d', font: { size: 9 } }, grid: { color: '#1e2636' } },
      y: { ticks: { color: '#6b7a8d' }, grid: { color: '#1e2636' }, beginAtZero: true },
    },
  }

  return (
    <div className="section fade-in">
      <div className="report-header"><span className="report-icon"></span><span>Final Report</span></div>

      {/* ─── Overall failure impact ─── */}
      <div className="subsection-label">All failed runs — total impact</div>
      <div className="summary-grid">
        <GlowCard value={grandTotal.total_runs} label="Total runs" cls="blue" />
        <GlowCard value={grandTotal.total_failed} label="Failed runs" cls={fr > 30 ? 'red' : 'amber'} />
        <GlowCard value={fmt3(fr) + '%'} label="Failure rate" cls={fr > 30 ? 'red' : fr > 10 ? 'amber' : 'green'} />
        <GlowCard value={fmt3(af.total_duration_minutes || 0) + ' min'} label="Failed run compute" cls="amber" />
        <GlowCard value={fmt3(af.total_energy_kwh || 0) + ' kWh'} label="Energy consumed" cls="amber" />
        <GlowCard value={fmt3(af.total_carbon_grams_co2 || 0) + 'g'} label="CO₂ emitted" cls="red" />
        <GlowCard value={'$' + fmt3(af.total_cost_usd || 0)} label="Est. cost wasted" cls="red" />
      </div>

      {/* ─── Carbon confidence range ─── */}
      {(af.total_carbon_grams_co2_lower || 0) > 0 && (
        <CarbonRange lower={af.total_carbon_grams_co2_lower} mid={af.total_carbon_grams_co2} upper={af.total_carbon_grams_co2_upper}
          intensity={grandTotal.carbon_intensity} methodology={af.methodology} />
      )}

      {/* ─── Impact comparisons ─── */}
      {impact.comparisons?.length > 0 && (
        <>
          <button className="btn-impact" onClick={() => setShowImpact(!showImpact)}>
            {showImpact ? '▾ Hide' : '▸ See your impact in context'}
          </button>
          {showImpact && (
            <div className="impact-grid fade-in">
              {impact.comparisons.map((c, i) => (
                <div key={i} className="impact-card">
                  <span className="impact-icon">{c.icon}</span>
                  <span className="impact-value">{fmt3(c.value)}</span>
                  <span className="impact-unit">{c.unit}</span>
                  <span className="impact-text">{c.text}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {/* ─── Top failing workflows ─── */}
      {topFailing.length > 0 && (
        <>
          <div className="subsection-label">Top failing workflows</div>
          <div className="card table-wrap">
            <table className="wf-table">
              <thead><tr><th>Workflow</th><th style={{textAlign:'center'}}>Failures</th></tr></thead>
              <tbody>
                {topFailing.map((w, i) => (
                  <tr key={i}>
                    <td><span className="mono">{w.name}</span></td>
                    <td style={{textAlign:'center'}}><span className="badge-fail">{w.failures}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* ─── Bar + donut charts ─── */}
      {wasteCounts.some(v => v > 0) && (
        <>
          <div className="chart-row">
            <div className="card chart-wrap">
              <Bar data={{ labels, datasets: [{ data: wasteCounts, backgroundColor: colors, borderRadius: 6, maxBarThickness: 50 }] }}
                   options={{ ...chartOpts, plugins: { ...chartOpts.plugins, title: { ...chartOpts.plugins.title, text: 'Wasted runs by category' } } }} />
            </div>
            <div className="card chart-wrap">
              <Bar data={{ labels, datasets: [{ data: energyVals, backgroundColor: colors, borderRadius: 6, maxBarThickness: 50 }] }}
                   options={{ ...chartOpts, plugins: { ...chartOpts.plugins, title: { ...chartOpts.plugins.title, text: 'Energy waste (Wh)' } } }} />
            </div>
          </div>
          <div className="donut-section">
            <div className="subsection-label">Waste distribution</div>
            <div className="card donut-wrap">
              <Doughnut data={{ labels, datasets: [{ data: wasteCounts, backgroundColor: colors, borderColor: '#0f1420', borderWidth: 2 }] }}
                options={{ responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'right', labels: { color: '#c5cdd8', font: { family: 'Outfit', size: 11 }, padding: 12 } } }, cutout: '65%' }} />
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function CarbonRange({ lower, mid, upper, intensity, methodology }) {
  const range = upper - lower
  const midPct = range > 0 ? ((mid - lower) / range) * 100 : 50
  return (
    <div className="carbon-range fade-in">
      <div className="subsection-label">Carbon confidence range</div>
      <div className="card carbon-range-card">
        <div className="cr-bar">
          <div className="cr-gradient" />
          <div className="cr-marker" style={{ left: midPct + '%' }}>
            <div className="cr-marker-dot" />
            <div className="cr-marker-label">{fmt3(mid)}g</div>
          </div>
        </div>
        <div className="cr-labels">
          <span className="cr-lo">{fmt3(lower)}g (best case)</span>
          <span className="cr-hi">{fmt3(upper)}g (worst case)</span>
        </div>
        {intensity && (
          <div className="cr-methodology">
            Using {fmt3(intensity)} g CO₂/kWh (equal-weighted avg across 11 Azure regions)
            {methodology?.sources && <span className="cr-sources"> · Sources: {methodology.sources.join(', ')}</span>}
          </div>
        )}
      </div>
    </div>
  )
}

function GlowCard({ value, label, cls }) {
  return (
    <div className={`card summary-card glow-${cls}`}>
      <div className={`big ${cls}`}>{value}</div>
      <div className="sub">{label}</div>
    </div>
  )
}
