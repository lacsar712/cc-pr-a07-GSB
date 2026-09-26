import { useCallback, useEffect, useState } from 'react'

function fmtTime(value) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

const STATE_TEXT = {
  active: '有效',
  used: '已使用',
  voided: '已作废',
  expired: '隔日失效',
}

const ACTION_TEXT = {
  generated: '生成',
  consumed: '使用入队',
  voided: '作废',
  expired: '隔日失效',
}

export default function App() {
  const [username, setUsername] = useState('printer')
  const [password, setPassword] = useState('print123456')
  const [token, setToken] = useState(localStorage.getItem('print_token') || '')
  const [role, setRole] = useState(localStorage.getItem('print_role') || '')
  const [view, setView] = useState('jobs')

  const api = useCallback(async (path, options = {}) => {
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
  }, [token])

  function logout() {
    localStorage.clear()
    setToken('')
    setRole('')
  }

  if (!token) {
    return <Login username={username} password={password}
      setUsername={setUsername} setPassword={setPassword}
      onLogin={(data) => {
        localStorage.setItem('print_token', data.access_token)
        localStorage.setItem('print_role', data.role)
        setToken(data.access_token)
        setRole(data.role)
      }} />
  }

  return (
    <main>
      <h1>印刷套准复核台</h1>
      <nav style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <button onClick={() => setView('jobs')} disabled={view === 'jobs'}>复核队列</button>
        <button onClick={() => setView('tokens')} disabled={view === 'tokens'}>墨路冲洗口令</button>
        <span style={{ marginLeft: 'auto' }}>
          {role === 'writer' ? '印刷员 printer' : '观察账号 checker'}
          <button onClick={logout} style={{ marginLeft: 8 }}>退出</button>
        </span>
      </nav>
      {view === 'jobs'
        ? <JobsView role={role} api={api} />
        : <TokensView role={role} api={api} />}
    </main>
  )
}

function Login({ username, password, setUsername, setPassword, onLogin }) {
  const [error, setError] = useState('')
  async function enter() {
    setError('')
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || '登录失败')
      onLogin(data)
    } catch (err) {
      setError(err.message)
    }
  }
  return (
    <main>
      <h1>印刷套准复核台</h1>
      <p>投递必须携带当日仍有效的墨路冲洗口令；错令、隔日旧令一律退回。</p>
      <input value={username} onChange={(e) => setUsername(e.target.value)} />
      <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
      <button onClick={enter}>登录</button>
      {error && <p style={{ color: 'crimson' }}>{error}</p>}
      <p>printer / print123456 可投递、生成与作废旧令；checker / check123456 只看</p>
    </main>
  )
}

function JobsView({ role, api }) {
  const [rows, setRows] = useState([])
  const [sheet, setSheet] = useState('插页-02')
  const [cyan, setCyan] = useState('0.08')
  const [magenta, setMagenta] = useState('0.02')
  const [rinseToken, setRinseToken] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setRows(await api('/api/jobs'))
  }, [api])

  useEffect(() => {
    load().catch(() => {})
    const timer = setInterval(() => load().catch(() => {}), 1000)
    return () => clearInterval(timer)
  }, [load])

  async function send() {
    setError('')
    setNotice('')
    try {
      await api('/api/jobs', {
        method: 'POST',
        body: JSON.stringify({
          sheet,
          cyan_mm: Number(cyan),
          magenta_mm: Number(magenta),
          rinse_token: rinseToken.trim(),
        }),
      })
      setNotice('投递成功，已进待处理；该口令已记为已用')
      setRinseToken('')
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <section>
      <h2>复核队列</h2>
      {role === 'writer' ? (
        <fieldset>
          <legend>投递复核（须填当日有效冲洗口令）</legend>
          <p>
            <label>印张 <input value={sheet} onChange={(e) => setSheet(e.target.value)} /></label>
            <label>青偏差(mm) <input value={cyan} onChange={(e) => setCyan(e.target.value)} /></label>
            <label>品偏差(mm) <input value={magenta} onChange={(e) => setMagenta(e.target.value)} /></label>
          </p>
          <p>
            <label>墨路冲洗口令 <input value={rinseToken} onChange={(e) => setRinseToken(e.target.value)}
              placeholder="例如 MR20260926-AB12CD" style={{ width: 260 }} /></label>
            <button onClick={send}>送复核</button>
          </p>
          {error && <p style={{ color: 'crimson' }}>退回：{error}</p>}
          {notice && <p style={{ color: 'seagreen' }}>{notice}</p>}
        </fieldset>
      ) : (
        <p>观察账号只读，不能投递。</p>
      )}
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
              <td>{row.status === 'pending' ? '待处理' : row.status === 'running' ? '复核中' : row.status}</td>
              <td>{row.verdict || '等待'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function TokensView({ role, api }) {
  const [data, setData] = useState({ active: [], records: [], events: [] })
  const [voidReason, setVoidReason] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    setData(await api('/api/rinse-tokens'))
  }, [api])

  useEffect(() => {
    load().catch(() => {})
    const timer = setInterval(() => load().catch(() => {}), 2000)
    return () => clearInterval(timer)
  }, [load])

  async function generate() {
    setError('')
    setNotice('')
    try {
      const row = await api('/api/rinse-tokens/generate', { method: 'POST' })
      setNotice(`已生成今日口令 ${row.code}`)
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  async function voidToken(id) {
    setError('')
    setNotice('')
    try {
      await api(`/api/rinse-tokens/${id}/void`, {
        method: 'POST',
        body: JSON.stringify({ reason: voidReason }),
      })
      setNotice('口令已作废并写入流水')
      setVoidReason('')
      await load()
    } catch (err) {
      setError(err.message)
    }
  }

  const writer = role === 'writer'

  return (
    <section>
      <h2>墨路冲洗口令</h2>

      {writer && (
        <fieldset>
          <legend>生成区（仅印刷员）</legend>
          <button onClick={generate}>生成今日口令</button>
          <label style={{ marginLeft: 12 }}>
            作废原因（可选）
            <input value={voidReason} onChange={(e) => setVoidReason(e.target.value)}
              placeholder="如：换墨重洗" style={{ marginLeft: 6 }} />
          </label>
          <p style={{ fontSize: 13, color: '#555' }}>
            每日仅一道有效口令；重新生成前须先作废当前口令。成功投递后口令立即记为已用，不可再次使用。
          </p>
        </fieldset>
      )}

      {error && <p style={{ color: 'crimson' }}>{error}</p>}
      {notice && <p style={{ color: 'seagreen' }}>{notice}</p>}

      <h3>有效口令（当日）</h3>
      {data.active.length === 0 ? (
        <p>今日尚无有效口令{writer ? '，请在上方生成。' : '。'}</p>
      ) : (
        <ul>
          {data.active.map((t) => (
            <li key={t.id}>
              <code style={{ fontSize: 16 }}>{t.code}</code>
              <span style={{ marginLeft: 8 }}>{t.token_date} · 生成 {fmtTime(t.created_at)} · {t.created_by}</span>
              {writer && <button style={{ marginLeft: 12 }} onClick={() => voidToken(t.id)}>作废</button>}
            </li>
          ))}
        </ul>
      )}

      <h3>作废记录</h3>
      {data.records.filter((t) => t.state === 'voided' || t.state === 'expired').length === 0 ? (
        <p>暂无作废记录。</p>
      ) : (
        <table>
          <thead>
            <tr><th>口令</th><th>归属日期</th><th>状态</th><th>时间</th><th>操作人</th><th>原因</th></tr>
          </thead>
          <tbody>
            {data.records
              .filter((t) => t.state === 'voided' || t.state === 'expired')
              .map((t) => (
                <tr key={t.id}>
                  <td><code>{t.code}</code></td>
                  <td>{t.token_date}</td>
                  <td>{STATE_TEXT[t.state] || t.state}</td>
                  <td>{fmtTime(t.voided_at || t.used_at || t.created_at)}</td>
                  <td>{t.voided_by || t.created_by}</td>
                  <td>{t.void_reason || (t.state === 'expired' ? '隔日未用' : '')}</td>
                </tr>
              ))}
          </tbody>
        </table>
      )}

      <h3>口令流水</h3>
      <table>
        <thead>
          <tr><th>时间</th><th>口令</th><th>动作</th><th>明细</th><th>操作人</th></tr>
        </thead>
        <tbody>
          {data.events.map((e) => (
            <tr key={e.id}>
              <td>{fmtTime(e.created_at)}</td>
              <td><code>{e.code}</code></td>
              <td>{ACTION_TEXT[e.action] || e.action}</td>
              <td>{e.detail}</td>
              <td>{e.actor}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
