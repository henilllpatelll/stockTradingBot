import { useCallback, useState, useEffect } from 'react';
import { Target, TrendingUp, AlertCircle, RefreshCw, Zap, Bot } from 'lucide-react';
import '../App.css';

interface ScannerCandidate {
  symbol: string;
  price: number;
  gap_pct: number;
  rvol: number;
  float_shares: number;
  market_cap: number;
  catalyst: string;
  verdict: string;
  scan_time: string;
}

interface ScannerData {
  status: string;
  last_updated: string;
  candidates: ScannerCandidate[];
}

export default function Scanner() {
  const [data, setData] = useState<ScannerData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [trading, setTrading] = useState<string | null>(null);
  const [tradeMsg, setTradeMsg] = useState<{ symbol: string; msg: string; ok: boolean } | null>(null);
  const [autoMode, setAutoMode] = useState(false);
  const [autoLoading, setAutoLoading] = useState(false);
  const [tradedToday, setTradedToday] = useState<string[]>([]);

  const tradeTicker = async (symbol: string) => {
    setTrading(symbol);
    setTradeMsg(null);
    try {
      const res = await fetch('http://127.0.0.1:8001/api/trade', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol, skip_filters: true, strategy: 'technical' }),
      });
      const json = await res.json();
      setTradeMsg({ symbol, msg: json.message, ok: json.status === 'ok' });
    } catch {
      setTradeMsg({ symbol, msg: 'API unreachable', ok: false });
    } finally {
      setTrading(null);
      setTimeout(() => setTradeMsg(null), 4000);
    }
  };

  const fetchScanner = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch('http://127.0.0.1:8001/api/scanner');
      if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch {
      setError('Cannot connect — make sure python api.py is running');
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchAutoMode = useCallback(async () => {
    try {
      const res = await fetch('http://127.0.0.1:8001/api/auto-mode');
      if (res.ok) {
        const json = await res.json();
        setAutoMode(json.enabled);
        setTradedToday(json.traded_today ?? []);
      }
    } catch {
      return;
    }
  }, []);

  const toggleAutoMode = async () => {
    setAutoLoading(true);
    try {
      const res = await fetch('http://127.0.0.1:8001/api/auto-mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: !autoMode }),
      });
      if (res.ok) {
        const json = await res.json();
        setAutoMode(json.enabled);
      }
    } catch {
      setError('Cannot update auto mode right now');
    } finally {
      setAutoLoading(false);
    }
  };

  useEffect(() => {
    const refresh = () => {
      void fetchScanner();
      void fetchAutoMode();
    };
    const initialId = setTimeout(refresh, 0);
    const intervalId = setInterval(refresh, 30000);
    return () => {
      clearTimeout(initialId);
      clearInterval(intervalId);
    };
  }, [fetchAutoMode, fetchScanner]);

  const formatMillions = (val: number) => `${(val / 1_000_000).toFixed(1)}M`;
  const getBadgeClass = (verdict: string) => verdict === 'GO' ? 'badge-success' : 'badge-danger';

  return (
    <div className="animate-fade-in">
      <div className="header" style={{ marginBottom: '1.5rem', paddingBottom: '1rem' }}>
        <div className="header-title">
          <h2 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#fff', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <Target className="text-accent-secondary" /> Premarket Scanner
          </h2>
          <p style={{ color: 'var(--text-muted)' }}>Live gappers — price $2-$20 · float &lt;10M · gap ≥10% · RVOL ≥5x</p>
        </div>
        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
          <button
            className="primary-btn"
            onClick={toggleAutoMode}
            disabled={autoLoading}
            style={{
              background: autoMode ? 'rgba(74,222,128,0.15)' : 'var(--bg-tertiary)',
              color: autoMode ? '#4ade80' : '#fff',
              border: autoMode ? '1px solid rgba(74,222,128,0.4)' : '1px solid transparent',
              boxShadow: autoMode ? '0 0 12px rgba(74,222,128,0.25)' : 'none',
              display: 'flex', alignItems: 'center', gap: '0.4rem',
            }}
          >
            <Bot size={16} className={autoMode ? 'animate-pulse' : ''} />
            Auto {autoMode ? 'ON' : 'OFF'}
          </button>
          <button className="primary-btn" onClick={fetchScanner} disabled={loading} style={{ background: 'var(--bg-tertiary)', color: '#fff' }}>
            <RefreshCw className={loading ? 'animate-spin' : ''} size={16} /> Refresh
          </button>
        </div>
      </div>

      {autoMode && (
        <div className="glass-panel" style={{ padding: '0.75rem 1rem', marginBottom: '1.5rem', background: 'rgba(74,222,128,0.06)', border: '1px solid rgba(74,222,128,0.2)', display: 'flex', alignItems: 'center', gap: '1rem', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: '#4ade80', fontWeight: 600 }}>
            <Bot size={16} /> Autonomous mode active — GO candidates will auto-trade (7–11 AM ET, max 3 concurrent)
          </div>
          {tradedToday.length > 0 && (
            <div style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
              Traded today: {tradedToday.join(', ')}
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="glass-panel text-danger" style={{ padding: '1rem', marginBottom: '1.5rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <AlertCircle size={20} /> {error}
        </div>
      )}

      {data && (
        <div style={{ marginBottom: '1rem', color: 'var(--text-muted)', fontSize: '0.875rem' }}>
          Last scan: {data.last_updated} &nbsp;·&nbsp;
          <span className="text-success">{data.candidates.length} candidates</span>
        </div>
      )}

      {data?.candidates.length === 0 && !loading && (
        <div className="glass-panel" style={{ padding: '4rem', textAlign: 'center', color: 'var(--text-muted)' }}>
          <Target size={48} style={{ margin: '0 auto 1rem', opacity: 0.2 }} />
          <p>No candidates passing filters right now. Scanner refreshes every 30s.</p>
        </div>
      )}

      <div className="dashboard-grid" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(350px, 1fr))' }}>
        {loading && !data && Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="glass-panel" style={{ height: '200px', animation: 'pulse 1.5s infinite', background: 'var(--bg-tertiary)' }} />
        ))}

        {data?.candidates.map((cand, idx) => {
          const wasAutoTraded = tradedToday.includes(cand.symbol);
          return (
          <div key={idx} className="glass-panel scanner-card" style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1rem', opacity: wasAutoTraded ? 0.65 : 1 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <div>
                <h3 style={{ fontSize: '1.5rem', fontWeight: 800, color: 'var(--text-primary)', margin: 0 }}>{cand.symbol}</h3>
                <div style={{ fontSize: '1.25rem', color: 'var(--text-secondary)' }}>${cand.price.toFixed(2)}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div className="text-success" style={{ fontSize: '1.25rem', fontWeight: 700, display: 'flex', alignItems: 'center', gap: '0.25rem', justifyContent: 'flex-end' }}>
                  +{cand.gap_pct.toFixed(1)}% <TrendingUp size={16} />
                </div>
                <div style={{ display: 'flex', gap: '0.35rem', marginTop: '0.5rem', justifyContent: 'flex-end', flexWrap: 'wrap' }}>
                  <span className={`badge ${getBadgeClass(cand.verdict)}`}>{cand.verdict}</span>
                  {wasAutoTraded && <span className="badge" style={{ background: 'rgba(99,102,241,0.2)', color: '#818cf8', border: '1px solid rgba(99,102,241,0.3)' }}>Auto-traded</span>}
                </div>
              </div>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '0.5rem', padding: '0.75rem', background: 'rgba(0,0,0,0.2)', borderRadius: 'var(--radius-md)' }}>
              <div>
                <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase' }}>RVOL</div>
                <div style={{ fontWeight: 600 }}>{cand.rvol.toFixed(1)}x</div>
              </div>
              <div>
                <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase' }}>Float</div>
                <div style={{ fontWeight: 600 }}>{formatMillions(cand.float_shares)}</div>
              </div>
              <div>
                <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', textTransform: 'uppercase' }}>Mkt Cap</div>
                <div style={{ fontWeight: 600 }}>${formatMillions(cand.market_cap)}</div>
              </div>
            </div>

            <div style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', borderTop: '1px solid var(--border-color)', paddingTop: '0.75rem' }}>
              {cand.catalyst}
            </div>

            <button
              className="trade-btn"
              onClick={() => tradeTicker(cand.symbol)}
              disabled={trading === cand.symbol}
            >
              <Zap size={14} fill="currentColor" />
              {trading === cand.symbol ? 'Queuing...' : 'Trade'}
            </button>

            {tradeMsg?.symbol === cand.symbol && (
              <div className={`trade-toast ${tradeMsg.ok ? 'toast-ok' : 'toast-err'}`}>
                {tradeMsg.msg}
              </div>
            )}
          </div>
          );
        })}
      </div>
      <style>{`
        .scanner-card { transition: all 0.2s; cursor: default; }
        .scanner-card:hover { transform: translateY(-4px); box-shadow: var(--shadow-glow); border-color: var(--accent-primary); }
        .trade-btn { display: flex; align-items: center; justify-content: center; gap: 0.4rem; width: 100%; padding: 0.6rem; border-radius: var(--radius-md); background: rgba(74,222,128,0.12); color: #4ade80; font-weight: 700; font-size: 0.875rem; border: 1px solid rgba(74,222,128,0.25); cursor: pointer; transition: all 0.2s; }
        .trade-btn:hover:not(:disabled) { background: rgba(74,222,128,0.22); box-shadow: 0 0 12px rgba(74,222,128,0.3); }
        .trade-btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .trade-toast { font-size: 0.8rem; padding: 0.4rem 0.75rem; border-radius: var(--radius-sm); text-align: center; }
        .toast-ok { color: #4ade80; background: rgba(74,222,128,0.1); }
        .toast-err { color: #f87171; background: rgba(248,113,113,0.1); }
      `}</style>
    </div>
  );
}
