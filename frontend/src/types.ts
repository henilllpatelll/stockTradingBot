export interface Candle {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface NewsEvent {
  dt_utc: string;
  symbol: string;
  headline: string;
  source: string;
  date_label: string;
}

export interface CatalystEvaluation {
  go_no_go: boolean;
  confidence: number;
  catalyst_quality: 'strong' | 'medium' | 'weak' | 'negative';
  risk_flags: string[];
  reasoning: string;
}

export interface CatalystRow {
  event: NewsEvent;
  is_premarket: boolean;
  cat_type: string;
  evaluation: CatalystEvaluation;
  move_10s: number | null;
  move_30s: number | null;
  move_1m: number | null;
  move_5m: number | null;
  move_10m: number | null;
  vol_spike: number | null;
  move_to_open: number | null;
  high_to_open: number | null;
  low_to_open: number | null;
  candles: Candle[];
}

export interface AnalysisData {
  results: CatalystRow[];
  no_data_events: CatalystRow[];
  synthesis: string;
}
