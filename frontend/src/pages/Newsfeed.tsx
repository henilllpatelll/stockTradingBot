import { useState, useEffect, useRef } from 'react';
import { Rss, AlertCircle, Clock, Wifi, WifiOff } from 'lucide-react';
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
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = () => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;
    const ws = new WebSocket('ws://127.0.0.1:8000/ws/news');
    wsRef.current = ws;
    ws.onopen = () => { setConnected(true); setError(null); };
    ws.onmessage = (e) => {
      const data = JSON.parse(e.data);
      if (data.type === 'history') {
        setAlerts(data.alerts);
      } else if (data.type === 'alert') {
        const { type: _, ...alert } = data;
        setAlerts(prev => [alert, ...prev].slice(0, 100));
      }
    };
    ws.onclose = () => { setConnected(false); reconnectRef.current = setTimeout(connect, 3000); };
    ws.onerror = () => { setError('Cannot connect — make sure python api.py is running'); setConnected(false); };
  };

  useEffect(() => {
    connect();
    return () => { reconnectRef.current && clearTimeout(reconnectRef.current); wsRef.current?.close(); };
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
            <Rss className="text-accent-primary" /> Live Newsfeed
          </h2>
          <p style={{ color: 'var(--text-muted)' }}>Alpaca WebSocket — alerts fire the moment a headline arrives</p>
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
            </div>
          </div>
        ))}
      </div>
      <style>{`.news-card { transition: transform 0.2s; } .news-card:hover { transform: translateX(5px); border-left: 4px solid var(--accent-primary); }`}</style>
    </div>
  );
}
