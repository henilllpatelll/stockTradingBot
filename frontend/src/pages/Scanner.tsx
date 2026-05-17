import { useState, useEffect } from 'react';
import { Target, TrendingUp, AlertCircle, RefreshCw } from 'lucide-react';
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

  const fetchScanner = async () => {
    setLoading(true);
    try {
      const res = await fetch('http://127.0.0.1:8000/api/scanner');
      if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch (err: any) {
      setError('Cannot connect — make sure python api.py is running');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchScanner();
    const id = setInterval(fetchScanner, 30000);
    return () => clearInterval(id);
  }, []);

  const formatMillions = (val: number) => `${(val / 1_000_000).toFixed(1)}M`;
  const getBadgeClass = (verdict: string) => verdict === 'GO' ? 'badge-success' : 'badge-danger';

  return (
    <div className="animate-fade-in">
      <div className="header" style={{ marginBottom: '1.5rem', paddingBottom: '1rem' }}>
        <div className="header-title">
          <h2 style={{ fontSize: '1.5rem', fontWeight: 700, color: '#fff', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <Target className="text-accent-secondary" /> Premarket Scanner
          </h2>
          <p style={{ color: 'var(--text-muted)' }}>Live gappers — price $1-$20 · float &lt;20M · gap ≥5% · RVOL ≥5x</p>
        </div>
        <button className="primary-btn" onClick={fetchScanner} disabled={loading} style={{ background: 'var(--bg-tertiary)', color: '#fff' }}>
          <RefreshCw className={loading ? 'animate-spin' : ''} size={16} /> Refresh
        </button>
      </div>

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

        {data?.candidates.map((cand, idx) => (
          <div key={idx} className="glass-panel scanner-card" style={{ padding: '1.5rem', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
              <div>
                <h3 style={{ fontSize: '1.5rem', fontWeight: 800, color: 'var(--text-primary)', margin: 0 }}>{cand.symbol}</h3>
                <div style={{ fontSize: '1.25rem', color: 'var(--text-secondary)' }}>${cand.price.toFixed(2)}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div className="text-success" style={{ fontSize: '1.25rem', fontWeight: 700, display: 'flex', alignItems: 'center', gap: '0.25rem', justifyContent: 'flex-end' }}>
                  +{cand.gap_pct.toFixed(1)}% <TrendingUp size={16} />
                </div>
                <span className={`badge ${getBadgeClass(cand.verdict)}`} style={{ marginTop: '0.5rem', display: 'inline-block' }}>{cand.verdict}</span>
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
          </div>
        ))}
      </div>
      <style>{`.scanner-card { transition: all 0.2s; cursor: default; } .scanner-card:hover { transform: translateY(-4px); box-shadow: var(--shadow-glow); border-color: var(--accent-primary); }`}</style>
    </div>
  );
}
