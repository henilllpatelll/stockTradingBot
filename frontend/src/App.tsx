import { useState, useMemo } from 'react';
import { Activity, Play, AlertCircle, BarChart2, X, Filter, BarChart, Target, Rss } from 'lucide-react';
import ReactApexChart from 'react-apexcharts';
import { format, parseISO } from 'date-fns';
import type { AnalysisData, CatalystRow } from './types';
import Scanner from './pages/Scanner';
import Newsfeed from './pages/Newsfeed';
import './App.css';

const API_URL = 'http://127.0.0.1:8000/api/analysis';

function formatPct(val: number | null): string {
  if (val === null) return '—';
  const sign = val >= 0 ? '+' : '';
  return `${sign}${val.toFixed(1)}%`;
}

function formatVol(val: number | null): string {
  if (val === null) return '—';
  return `${val.toFixed(1)}x`;
}

const getVerdictBadgeClass = (go: boolean) => {
  return go ? 'badge-success' : 'badge-danger';
};

export default function App() {
  const [activeTab, setActiveTab] = useState<'analysis' | 'scanner' | 'news'>('analysis');
  const [data, setData] = useState<AnalysisData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedRow, setSelectedRow] = useState<CatalystRow | null>(null);
  const [filterVerdict, setFilterVerdict] = useState('ALL');
  const [filterType, setFilterType] = useState('ALL');
  const [filterDate, setFilterDate] = useState('ALL');

  const runAnalysis = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(API_URL);
      if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
      const json = await res.json();
      setData(json);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  // Removed auto-analysis on mount based on user request

  const filteredResults = useMemo(() => {
    if (!data) return [];
    return data.results.filter(row => {
      const verdictMatch = filterVerdict === 'ALL' || (
        filterVerdict === 'GO' ? row.evaluation.go_no_go :
        filterVerdict === 'NO-GO' ? !row.evaluation.go_no_go : true
      );
      const typeMatch = filterType === 'ALL' || row.cat_type === filterType;
      const dateMatch = filterDate === 'ALL' || row.event.date_label === filterDate;
      return verdictMatch && typeMatch && dateMatch;
    });
  }, [data, filterVerdict, filterType, filterDate]);

  const uniqueDates = useMemo(() => {
    if (!data) return [];
    const dates = new Set(data.results.map(r => r.event.date_label));
    return Array.from(dates);
  }, [data]);

  const uniqueTypes = useMemo(() => {
    if (!data) return [];
    const types = new Set(data.results.map(r => r.cat_type));
    return Array.from(types).sort();
  }, [data]);

  const summaryStats = useMemo(() => {
    if (!data) return null;
    const goSignals = data.results.filter(r => r.evaluation.go_no_go);
    const avgGoMove = goSignals.reduce((acc, r) => acc + (r.move_5m || 0), 0) / (goSignals.length || 1);
    
    return {
      total: data.results.length,
      longSignals: goSignals.length,
      avgLongMove: avgGoMove,
      noData: data.no_data_events.length
    };
  }, [data]);

  const chartOptions = useMemo(() => {
    return {
      chart: {
        type: 'candlestick' as const,
        background: 'transparent',
        toolbar: { show: false },
        animations: { enabled: false }
      },
      theme: { mode: 'dark' as const },
      xaxis: {
        type: 'datetime' as const,
        labels: { style: { colors: '#a0a0b0' } },
        axisBorder: { show: false },
        axisTicks: { show: false }
      },
      yaxis: {
        tooltip: { enabled: true },
        labels: { style: { colors: '#a0a0b0' } }
      },
      grid: {
        borderColor: 'rgba(255,255,255,0.05)',
        strokeDashArray: 4,
      },
      plotOptions: {
        candlestick: {
          colors: { upward: '#4ade80', downward: '#f87171' },
          wick: { useFillColor: true }
        }
      }
    };
  }, []);

  const chartSeries = useMemo(() => {
    if (!selectedRow || !selectedRow.candles.length) return [];
    return [{
      data: selectedRow.candles.map(c => ({
        x: new Date(c.timestamp).getTime(),
        y: [c.open, c.high, c.low, c.close]
      }))
    }];
  }, [selectedRow]);

  return (
    <div className="app-container">
      <nav className="glass-panel" style={{ marginBottom: '2rem', padding: '0.5rem', display: 'flex', gap: '0.5rem', borderRadius: 'var(--radius-full)' }}>
        <button 
          className={`nav-btn ${activeTab === 'analysis' ? 'active' : ''}`}
          onClick={() => setActiveTab('analysis')}
        >
          <BarChart size={18} /> Analyzer
        </button>
        <button 
          className={`nav-btn ${activeTab === 'scanner' ? 'active' : ''}`}
          onClick={() => setActiveTab('scanner')}
        >
          <Target size={18} /> Scanner
        </button>
        <button 
          className={`nav-btn ${activeTab === 'news' ? 'active' : ''}`}
          onClick={() => setActiveTab('news')}
        >
          <Rss size={18} /> Newsfeed
        </button>
      </nav>

      {activeTab === 'scanner' && <Scanner />}
      {activeTab === 'news' && <Newsfeed />}

      {activeTab === 'analysis' && (
        <div className="animate-fade-in">
          <header className="header">
            <div className="header-title">
              <h1>Benzinga Pro Catalyst Analyzer</h1>
              <p>Historical SIP data analysis and pattern recognition</p>
            </div>
            <button 
              className="primary-btn" 
              onClick={runAnalysis} 
              disabled={loading}
            >
              {loading ? <Activity className="animate-spin" size={18} /> : <Play size={18} fill="currentColor" />}
              {loading ? 'Analyzing...' : 'Run Analysis'}
            </button>
          </header>

          {error && (
            <div className="glass-panel text-danger" style={{ padding: '1rem', marginBottom: '1.5rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <AlertCircle size={20} />
              {error} (Make sure the backend is running)
            </div>
          )}

          {data?.synthesis && (
            <div className="glass-panel" style={{ padding: '1.5rem', marginBottom: '1.5rem', whiteSpace: 'pre-wrap', lineHeight: '1.6' }}>
              <h2 className="text-lg font-bold" style={{ color: 'var(--accent-primary)', marginBottom: '0.5rem' }}>AI Synthesis (Claude Haiku)</h2>
              {data.synthesis}
            </div>
          )}

          <div className="dashboard-grid">
            <aside className="sidebar">
              {summaryStats && (
                <>
                  <div className="glass-panel metric-card">
                    <span className="metric-title">Total Catalysts</span>
                    <span className="metric-value">{summaryStats.total}</span>
                  </div>
                  <div className="glass-panel metric-card">
                    <span className="metric-title">Tradeable GOs</span>
                    <span className="metric-value text-success">{summaryStats.longSignals}</span>
                  </div>
                  <div className="glass-panel metric-card">
                    <span className="metric-title">Avg T+5m on GOs</span>
                    <span className="metric-value text-success">{formatPct(summaryStats.avgLongMove)}</span>
                  </div>
                  <div className="glass-panel metric-card">
                    <span className="metric-title">Events without Data</span>
                    <span className="metric-value text-muted">{summaryStats.noData}</span>
                  </div>
                </>
              )}
            </aside>

            <main>
              <div className="filters-bar animate-fade-in">
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', color: 'var(--text-muted)' }}>
                  <Filter size={18} /> Filters:
                </div>
                <select 
                  className="filter-select" 
                  value={filterDate} 
                  onChange={e => setFilterDate(e.target.value)}
                >
                  <option value="ALL">All Dates</option>
                  {uniqueDates.map(d => <option key={d} value={d}>{d}</option>)}
                </select>
                <select 
                  className="filter-select" 
                  value={filterVerdict} 
                  onChange={e => setFilterVerdict(e.target.value)}
                >
                  <option value="ALL">All Verdicts</option>
                  <option value="GO">GO</option>
                  <option value="NO-GO">NO-GO</option>
                </select>
                <select 
                  className="filter-select" 
                  value={filterType} 
                  onChange={e => setFilterType(e.target.value)}
                >
                  <option value="ALL">All Catalyst Types</option>
                  {uniqueTypes.map(t => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>

              <div className="glass-panel table-container animate-fade-in" style={{ animationDelay: '0.1s' }}>
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>DATE</th>
                      <th>TIME (ET)</th>
                      <th>TICKER</th>
                      <th>CATALYST TYPE</th>
                      <th>T+1m</th>
                      <th>T+5m</th>
                      <th>VOL SPIKE</th>
                      <th>AI VERDICT</th>
                      <th>CONFIDENCE</th>
                    </tr>
                  </thead>
                  <tbody>
                    {!data && loading && (
                      <tr><td colSpan={9} style={{ textAlign: 'center', padding: '2rem' }}>Loading data...</td></tr>
                    )}
                    {filteredResults.map((row, i) => {
                      const dateEt = format(parseISO(row.event.dt_utc), "MMM dd, yyyy");
                      const timeEt = format(parseISO(row.event.dt_utc), "hh:mm a");
                      return (
                        <tr key={i} onClick={() => setSelectedRow(row)}>
                          <td><span className="text-muted">{dateEt}</span></td>
                          <td>{timeEt} {row.is_premarket && <span className="text-muted text-xs">[PM]</span>}</td>
                          <td className="font-bold text-secondary">{row.event.symbol}</td>
                          <td>{row.cat_type}</td>
                          <td className={row.move_1m && row.move_1m > 0 ? 'text-success' : row.move_1m && row.move_1m < 0 ? 'text-danger' : ''}>
                            {formatPct(row.move_1m)}
                          </td>
                          <td className={row.move_5m && row.move_5m > 0 ? 'text-success' : row.move_5m && row.move_5m < 0 ? 'text-danger' : ''}>
                            {formatPct(row.move_5m)}
                          </td>
                          <td>{formatVol(row.vol_spike)}</td>
                          <td>
                            <span className={`badge ${getVerdictBadgeClass(row.evaluation.go_no_go)}`}>
                              {row.evaluation.go_no_go ? 'GO' : 'NO-GO'}
                            </span>
                          </td>
                          <td className="text-xs">
                            {Math.round(row.evaluation.confidence * 100)}% ({row.evaluation.catalyst_quality})
                          </td>
                        </tr>
                      )
                    })}
                    {filteredResults.length === 0 && data && !loading && (
                      <tr><td colSpan={9} style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>No catalysts found matching filters.</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </main>
          </div>

          {/* Candlestick Modal */}
          {selectedRow && (
            <div className="modal-overlay" onClick={() => setSelectedRow(null)}>
              <div className="modal-content" onClick={e => e.stopPropagation()}>
                <div className="modal-header">
                  <div className="header-title">
                    <h2 className="text-xl" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                      <BarChart2 className="text-success" />
                      {selectedRow.event.symbol} Analysis
                    </h2>
                    <p>{selectedRow.event.headline}</p>
                  </div>
                  <button className="close-btn" onClick={() => setSelectedRow(null)}>
                    <X size={24} />
                  </button>
                </div>
                <div className="modal-body">
                  <div style={{ display: 'flex', gap: '1rem', marginBottom: '1.5rem', flexWrap: 'wrap' }}>
                    <span className={`badge ${getVerdictBadgeClass(selectedRow.evaluation.go_no_go)} text-base`}>
                      {selectedRow.evaluation.go_no_go ? 'GO' : 'NO-GO'} ({Math.round(selectedRow.evaluation.confidence * 100)}%)
                    </span>
                    <span className="badge badge-neutral text-base">{selectedRow.cat_type}</span>
                    <span className="badge badge-neutral text-base">Quality: {selectedRow.evaluation.catalyst_quality}</span>
                    <span className="badge badge-neutral text-base">Vol: {formatVol(selectedRow.vol_spike)}</span>
                  </div>
                  <div className="glass-panel" style={{ padding: '1rem', marginBottom: '1.5rem', fontSize: '0.875rem' }}>
                    <p><strong>Reasoning:</strong> {selectedRow.evaluation.reasoning}</p>
                    {selectedRow.evaluation.risk_flags.length > 0 && (
                      <p style={{ marginTop: '0.5rem', color: 'var(--accent-warning)' }}>
                        <strong>Risk Flags:</strong> {selectedRow.evaluation.risk_flags.join(', ')}
                      </p>
                    )}
                  </div>
                  
                  {selectedRow.candles.length > 0 ? (
                    <div style={{ height: 400, width: '100%', background: 'rgba(0,0,0,0.2)', borderRadius: 'var(--radius-md)' }}>
                      <ReactApexChart 
                        options={chartOptions} 
                        series={chartSeries} 
                        type="candlestick" 
                        height={400} 
                      />
                    </div>
                  ) : (
                    <div className="glass-panel" style={{ padding: '3rem', textAlign: 'center', color: 'var(--text-muted)' }}>
                      <AlertCircle size={48} style={{ margin: '0 auto 1rem', opacity: 0.5 }} />
                      <p>No 1-minute candlestick data available for this event.</p>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
      <style>{`
        .nav-btn {
          flex: 1;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 0.5rem;
          padding: 0.75rem 1rem;
          border-radius: var(--radius-full);
          color: var(--text-secondary);
          font-weight: 600;
          transition: all 0.2s;
        }
        .nav-btn:hover {
          color: var(--text-primary);
          background: rgba(255,255,255,0.05);
        }
        .nav-btn.active {
          color: #000;
          background: var(--text-primary);
          box-shadow: 0 4px 12px rgba(255,255,255,0.2);
        }
      `}</style>
    </div>
  );
}
