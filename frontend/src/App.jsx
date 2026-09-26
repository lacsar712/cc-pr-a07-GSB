import { useEffect, useState } from 'react'

export default function App() {
  const [username, setUsername] = useState('printer')
  const [password, setPassword] = useState('print123456')
  const [token, setToken] = useState(localStorage.getItem('print_token') || '')
  const [role, setRole] = useState(localStorage.getItem('print_role') || '')
  const [view, setView] = useState('jobs')
  const [rows, setRows] = useState([])
  const [flushTokens, setFlushTokens] = useState([])
  const [events, setEvents] = useState([])
  const [sheet, setSheet] = useState('插页-02')
  const [cyan, setCyan] = useState('0.08')
  const [magenta, setMagenta] = useState('0.02')
  const [flushToken, setFlushToken] = useState('')
  const [generated, setGenerated] = useState(null)
  const [error, setError] = useState('')
  const [tokenError, setTokenError] = useState('')

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data.detail || '请求失败')
    return data
  }

  async function load() {
    setRows(await api('/api/jobs'))
    setFlushTokens(await api('/api/flush-tokens'))
    setEvents(await api('/api/flush-tokens/events'))
  }

  useEffect(() => {
    if (!token) return
    load()
    const timer = setInterval(load, 1000)
    return () => clearInterval(timer)
  }, [token])

  async function enter() {
    const data = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
    localStorage.setItem('print_token', data.access_token)
    localStorage.setItem('print_role', data.role)
    setToken(data.access_token)
    setRole(data.role)
  }

  async function send() {
    setError('')
    try {
      await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify({
          sheet,
          cyan_mm: Number(cyan),
          magenta_mm: Number(magenta),
          flush_token: flushToken,
        }),
      })
    } catch (err) {
      setError(err.message)
    }
  }

  async function generate() {
    setTokenError('')
    try {
      const row = await api('/api/flush-tokens', { method: 'POST' })
      setGenerated(row)
      setFlushToken(row.token)
    } catch (err) {
      setTokenError(err.message)
    }
  }

  function leave() {
    localStorage.clear()
    setToken('')
    setRole('')
  }

  if (!token) {
    return (
      <main>
        <h1>印刷套准复核台</h1>
        <p>提交后接口只入队。另一进程领走偏差并写结论，页面轮询到结论出现。投递须填当日冲洗口令。</p>
        <input value={username} onChange={(e) => setUsername(e.target.value)} />
        <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        <button onClick={enter}>登录</button>
        <p>printer / print123456 可生成口令并送复核；checker / check123456 只看</p>
      </main>
    )
  }

  const today = new Date().toISOString().slice(0, 10)
  const activeTokens = flushTokens.filter((t) => t.status === 'active' && t.valid_date === today)
  const usedEvents = events.filter((e) => e.event === 'used')

  return (
    <main>
      <h1>印刷套准复核台</h1>
      <nav>
        <button onClick={() => setView('jobs')} disabled={view === 'jobs'}>复核台</button>
        <button onClick={() => setView('tokens')} disabled={view === 'tokens'}>冲洗口令</button>
        <button onClick={leave}>退出</button>
      </nav>
      {view === 'jobs' && (
        <>
          {role === 'writer' && (
            <p>
              <input value={sheet} onChange={(e) => setSheet(e.target.value)} />
              <input value={cyan} onChange={(e) => setCyan(e.target.value)} />
              <input value={magenta} onChange={(e) => setMagenta(e.target.value)} />
              <input
                value={flushToken}
                onChange={(e) => setFlushToken(e.target.value)}
                placeholder="当日冲洗口令"
              />
              <button onClick={send}>送复核</button>
            </p>
          )}
          {error && <p>{error}</p>}
          <table>
            <thead>
              <tr><th>印张</th><th>青</th><th>品</th><th>状态</th><th>结论</th></tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{row.sheet}</td>
                  <td>{row.cyan_mm}</td>
                  <td>{row.magenta_mm}</td>
                  <td>{row.status}</td>
                  <td>{row.verdict || '等待'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {view === 'tokens' && (
        <>
          <section>
            <h2>生成区</h2>
            {role === 'writer' ? (
              <>
                <button onClick={generate}>生成今日口令</button>
                {generated && (
                  <p>今日口令：{generated.token}（限 {generated.valid_date} 使用，成功投递后即作废）</p>
                )}
              </>
            ) : (
              <p>观察账号仅可查看口令与流水，不能生成也不能作废。</p>
            )}
            {tokenError && <p>{tokenError}</p>}
          </section>
          <section>
            <h2>有效口令</h2>
            <ul>
              {activeTokens.map((t) => (
                <li key={t.id}>{t.token} · 限 {t.valid_date} 使用 · {t.created_by} 生成</li>
              ))}
              {activeTokens.length === 0 && <li>暂无有效口令</li>}
            </ul>
          </section>
          <section>
            <h2>作废记录</h2>
            <ul>
              {usedEvents.map((e) => (
                <li key={e.id}>
                  {e.token} · {e.actor} 投递第 {e.job_id} 号印张后作废 · {new Date(e.created_at).toLocaleString()}
                </li>
              ))}
              {usedEvents.length === 0 && <li>暂无作废记录</li>}
            </ul>
          </section>
        </>
      )}
    </main>
  )
}
