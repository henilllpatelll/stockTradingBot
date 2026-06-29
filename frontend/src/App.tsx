import { useState } from 'react';
import { Rss, Target } from 'lucide-react';
import Scanner from './pages/Scanner';
import Newsfeed from './pages/Newsfeed';
import './App.css';

export default function App() {
  const [activeTab, setActiveTab] = useState<'news' | 'scanner'>('news');

  return (
    <div className="app-container">
      <nav className="glass-panel" style={{ marginBottom: '2rem', padding: '0.5rem', display: 'flex', gap: '0.5rem', borderRadius: 'var(--radius-full)' }}>
        <button
          className={`nav-btn ${activeTab === 'news' ? 'active' : ''}`}
          onClick={() => setActiveTab('news')}
        >
          <Rss size={18} /> Trade News Catalysts
        </button>
        <button
          className={`nav-btn ${activeTab === 'scanner' ? 'active' : ''}`}
          onClick={() => setActiveTab('scanner')}
        >
          <Target size={18} /> Scanner
        </button>
      </nav>

      {activeTab === 'news' && <Newsfeed />}
      {activeTab === 'scanner' && <Scanner />}

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
