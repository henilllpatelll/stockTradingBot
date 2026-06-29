import { useState, useEffect, useRef } from 'react';
import { Rss, AlertCircle, Clock, Wifi, WifiOff, Zap } from 'lucide-react';
import '../App.css';

interface NewsAlert {
  id: string;
  time: string;
  symbol: string;
  headline: string;
  source: string;
  price: number;
  gap_pct: number;
  rvol: number;
  impact: 'High' | 'Medium' | 'Low';
}

export default function Newsfeed() {
  const [alerts, setAlerts] = useState<NewsAlert[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trading, setTrading] = useState<string | null>(null);
  const [tradeMsg, setTradeMsg] = useState<{ id: string; msg: string; ok: boolean } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const tradeTicker = async (alert: NewsAlert) => {
    setTrading(alert.id);
    setTradeMsg(null);
    try {
      const res = await fetch('http://127.0.0.1:8001/api/trade', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: alert.symbol,
          skip_filters: true,
          headline: alert.headline,
          strategy: 'catalyst',
        }),
      });
      const json = await res.json();
      setTradeMsg({ id: alert.id, msg: json.message, ok: json.status === 'ok' });
    } catch {
      setTradeMsg({ id: alert.id, msg: 'API unreachable', ok: false });
    } finally {
      setTrading(null);
      setTimeout(() => setTradeMsg(null), 4000);
    }
  };

  useEffect(() => {
    const connect = () => {
      if (wsRef.current?.readyState === WebSocket.OPEN) return;
      const ws = new WebSocket('ws://127.0.0.1:8001/ws/news');
      wsRef.current = ws;
      ws.onopen = () => { setConnected(true); setError(null); };
      ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type === 'history') {
          setAlerts(data.alerts);
        } else if (data.type === 'alert') {
          const alert = Object.fromEntries(
            Object.entries(data).filter(([key]) => key !== 'type'),
          ) as unknown as NewsAlert;
          setAlerts(prev => [alert, ...prev].slice(0, 100));
        }
      };
      ws.onclose = () => { setConnected(false); reconnectRef.current = setTimeout(connect, 3000); };
      ws.onerror = () => { setError('Cannot connect - make sure python api.py is running'); setConnected(false); };
    };

    connect();
    return () => {
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, []);

  const getImpactColor = (impact: string) => {
    if (impact === 'High') return 'var(--accent-danger)';
    if (impact === 'Medium') return 'var(--accent-warning)';
    return 'var(--accent-primary)';
  };

  return (
    <div className="animate-fade-in">
      <div className="header" style={{ marginBottom: '1.5rem', paddingBottom: '1rem' }}>
        <div className="header-title">
          <h2 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#fff', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <Rss className="text-accent-primary" /> Trade News Catalysts
          </h2>
          <p style={{ color: 'var(--text-muted)' }}>Alpaca WebSocket + GlobeNewswire RSS — alerts fire the moment a headline arrives</p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: connected ? 'var(--accent-primary)' : 'var(--accent-danger)' }}>
          {connected ? <Wifi size={16} /> : <WifiOff size={16} />}
          {connected ? 'Live' : 'Disconnected'}
        </div>
      </div>

      {error && (
        <div className="glass-panel text-danger" style={{ padding: '1rem', marginBottom: '1.5rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <AlertCircle size={20} /> {error}
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem', maxWidth: '900px', margin: '0 auto' }}>
        {alerts.length === 0 && connected && (
          <div className="glass-panel" style={{ padding: '4rem', textAlign: 'center', color: 'var(--text-muted)' }}>
            <Rss size={48} style={{ margin: '0 auto 1rem', opacity: 0.2 }} />
            <p>Listening for headlines that pass your filters...</p>
          </div>
        )}
        {alerts.map((alert) => (
          <div key={alert.id} className="glass-panel news-card" style={{ padding: '1.5rem', display: 'flex', gap: '1.5rem', alignItems: 'center' }}>
            <div style={{ minWidth: '80px', textAlign: 'center' }}>
              <div style={{ fontSize: '1.5rem', fontWeight: 800, color: 'var(--text-primary)' }}>{alert.symbol}</div>
              <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '0.25rem', marginTop: '0.25rem' }}>
                <Clock size={12} /> {alert.time?.split(' ')[0]}
              </div>
            </div>
            <div style={{ width: '2px', height: '50px', background: 'var(--border-color)' }} />
            <div style={{ flex: 1 }}>
              <h3 style={{ fontSize: '1.05rem', fontWeight: 600, color: 'var(--text-primary)', margin: '0 0 0.5rem 0', lineHeight: 1.4 }}>
                {alert.headline}
              </h3>
              <div style={{ display: 'flex', gap: '1rem', fontSize: '0.85rem', flexWrap: 'wrap' }}>
                <span style={{ color: 'var(--text-muted)' }}>Source: <span style={{ color: 'var(--text-secondary)' }}>{alert.source}</span></span>
                {alert.price > 0 && <span style={{ color: 'var(--text-secondary)' }}>${alert.price?.toFixed(2)}</span>}
                {alert.gap_pct > 0 && <span className="text-success">+{alert.gap_pct?.toFixed(1)}%</span>}
                {alert.rvol > 0 && <span style={{ color: 'var(--text-secondary)' }}>{alert.rvol?.toFixed(1)}x RVOL</span>}
                <span style={{ color: getImpactColor(alert.impact), fontWeight: 600 }}>{alert.impact}</span>
              </div>
              {tradeMsg?.id === alert.id && (
                <div style={{ marginTop: '0.5rem', fontSize: '0.8rem', color: tradeMsg.ok ? '#4ade80' : '#f87171' }}>
                  {tradeMsg.msg}
                </div>
              )}
            </div>
            <button
              className="news-trade-btn"
              onClick={() => tradeTicker(alert)}
              disabled={trading === alert.id}
            >
              <Zap size={14} fill="currentColor" />
              {trading === alert.id ? '...' : 'Trade'}
            </button>
          </div>
        ))}
      </div>
      <style>{`
        .news-card { transition: transform 0.2s; }
        .news-card:hover { transform: translateX(5px); border-left: 4px solid var(--accent-primary); }
        .news-trade-btn { flex-shrink: 0; display: flex; align-items: center; gap: 0.35rem; padding: 0.5rem 0.85rem; border-radius: var(--radius-md); background: rgba(74,222,128,0.12); color: #4ade80; font-weight: 700; font-size: 0.8rem; border: 1px solid rgba(74,222,128,0.25); cursor: pointer; transition: all 0.2s; white-space: nowrap; }
        .news-trade-btn:hover:not(:disabled) { background: rgba(74,222,128,0.22); box-shadow: 0 0 10px rgba(74,222,128,0.25); }
        .news-trade-btn:disabled { opacity: 0.5; cursor: not-allowed; }
      `}</style>
    </div>
  );
}
