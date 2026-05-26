'use client';

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';

type Direction = 'up' | 'down' | 'flat';
type Side = 'buy' | 'sell';
type ConnectionStatus = 'connected' | 'reconnecting' | 'disconnected';

type WatchlistItem = {
  ticker: string;
  current_price: number;
  previous_price?: number;
  change_percent?: number;
  direction?: Direction;
  timestamp?: string;
};

type Position = {
  ticker: string;
  quantity: number;
  avg_cost: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  change_percent: number;
};

type Portfolio = {
  cash_balance: number;
  total_value: number;
  unrealized_pnl: number;
  positions: Position[];
};

type HistorySnapshot = {
  total_value: number;
  recorded_at: string;
};

type ChatMessage = {
  role: 'user' | 'assistant' | 'system';
  content: string;
  details?: string[];
};

type ChatActionError = string | { error?: string; details?: Record<string, unknown> };

type SparkSeries = Record<string, number[]>;
type FlashMap = Record<string, Direction>;

const FALLBACK_WATCHLIST: WatchlistItem[] = [
  { ticker: 'AAPL', current_price: 192.21, previous_price: 191.72, change_percent: 0.26, direction: 'up' },
  { ticker: 'MSFT', current_price: 428.67, previous_price: 430.14, change_percent: -0.34, direction: 'down' },
  { ticker: 'NVDA', current_price: 949.5, previous_price: 931.2, change_percent: 1.97, direction: 'up' },
  { ticker: 'AMZN', current_price: 183.88, previous_price: 184.04, change_percent: -0.09, direction: 'down' },
  { ticker: 'GOOGL', current_price: 176.42, previous_price: 175.9, change_percent: 0.3, direction: 'up' },
  { ticker: 'META', current_price: 474.99, previous_price: 477.5, change_percent: -0.53, direction: 'down' },
  { ticker: 'TSLA', current_price: 178.12, previous_price: 176.25, change_percent: 1.06, direction: 'up' },
  { ticker: 'JPM', current_price: 201.58, previous_price: 200.98, change_percent: 0.3, direction: 'up' },
  { ticker: 'V', current_price: 276.11, previous_price: 276.9, change_percent: -0.29, direction: 'down' },
  { ticker: 'SPY', current_price: 529.77, previous_price: 528.92, change_percent: 0.16, direction: 'up' }
];

const EMPTY_PORTFOLIO: Portfolio = {
  cash_balance: 100000,
  total_value: 100000,
  unrealized_pnl: 0,
  positions: []
};

const numberFormatter = new Intl.NumberFormat('en-US', {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2
});

const currencyFormatter = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2
});

function toNumber(value: unknown, fallback = 0) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function normalizeTicker(value: string) {
  return value.trim().toUpperCase().replace(/[^A-Z0-9.-]/g, '');
}

async function getJson<T>(path: string, fallback: T): Promise<T> {
  try {
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) return fallback;
    return (await response.json()) as T;
  } catch {
    return fallback;
  }
}

async function sendJson<T>(path: string, body: unknown, method = 'POST'): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: method === 'DELETE' ? undefined : JSON.stringify(body)
  });
  const payload = (await response.json().catch(() => ({}))) as T;
  if (!response.ok) {
    const error = payload && typeof payload === 'object' && 'error' in payload ? String(payload.error) : 'Request failed';
    throw new Error(error);
  }
  return payload;
}

function mergeSeries(series: number[], next: number, max = 80) {
  const clean = Number.isFinite(next) ? next : series.at(-1) ?? 0;
  return [...series, clean].slice(-max);
}

function percentClass(value = 0) {
  if (value > 0) return 'positive';
  if (value < 0) return 'negative';
  return 'neutral';
}

function formatChatError(error: ChatActionError) {
  if (typeof error === 'string') return error;
  const ticker = error.details && 'ticker' in error.details ? ` for ${String(error.details.ticker)}` : '';
  return `${error.error ?? 'action_error'}${ticker}`;
}

function directionFromPrices(current: number, previous?: number): Direction {
  if (!previous || current === previous) return 'flat';
  return current > previous ? 'up' : 'down';
}

function Sparkline({
  values,
  width = 118,
  height = 34,
  stroke = '#209dd7'
}: {
  values: number[];
  width?: number;
  height?: number;
  stroke?: string;
}) {
  const points = useMemo(() => buildLinePoints(values, width, height, 4), [height, values, width]);
  const fill = points ? `${points} ${width},${height} 0,${height}` : '';

  return (
    <svg className="sparkline" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Price sparkline">
      {points ? <polygon points={fill} className="chart-fill" /> : null}
      {points ? <polyline points={points} fill="none" stroke={stroke} strokeWidth="2" /> : null}
    </svg>
  );
}

function LineChart({
  values,
  labels,
  tone = 'blue',
  emptyLabel = 'Awaiting data'
}: {
  values: number[];
  labels?: string[];
  tone?: 'blue' | 'yellow' | 'purple';
  emptyLabel?: string;
}) {
  const width = 760;
  const height = 258;
  const points = useMemo(() => buildLinePoints(values, width, height, 18), [values]);
  const accent = tone === 'yellow' ? '#ecad0a' : tone === 'purple' ? '#b05ad0' : '#209dd7';
  const lastValue = values.at(-1);

  return (
    <div className="line-chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Time series chart">
        <defs>
          <linearGradient id={`fill-${tone}`} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={accent} stopOpacity="0.34" />
            <stop offset="100%" stopColor={accent} stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3].map((row) => (
          <line key={row} x1="0" x2={width} y1={(height / 4) * row + 10} y2={(height / 4) * row + 10} className="grid-line" />
        ))}
        {points ? (
          <>
            <polygon points={`${points} ${width - 18},${height - 18} 18,${height - 18}`} fill={`url(#fill-${tone})`} />
            <polyline points={points} fill="none" stroke={accent} strokeWidth="3" strokeLinecap="round" />
          </>
        ) : null}
      </svg>
      {!points ? <div className="chart-empty">{emptyLabel}</div> : null}
      {lastValue !== undefined ? (
        <div className="chart-caption">
          <span>{labels?.at(-1) ?? 'Latest'}</span>
          <strong>{currencyFormatter.format(lastValue)}</strong>
        </div>
      ) : null}
    </div>
  );
}

function buildLinePoints(values: number[], width: number, height: number, padding: number) {
  if (values.length < 2) return '';
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  return values
    .map((value, index) => {
      const x = padding + (index / Math.max(values.length - 1, 1)) * (width - padding * 2);
      const y = padding + (1 - (value - min) / range) * (height - padding * 2);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(' ');
}

function PortfolioHeatmap({ positions, totalValue }: { positions: Position[]; totalValue: number }) {
  const active = positions.filter((position) => position.market_value > 0);

  if (!active.length) {
    return <div className="empty-panel">No positions yet</div>;
  }

  const totalMarketValue = active.reduce((sum, position) => sum + position.market_value, 0) || totalValue || 1;

  return (
    <div className="heatmap">
      {active.map((position) => {
        const weight = Math.max(12, (position.market_value / totalMarketValue) * 100);
        const colorClass = position.unrealized_pnl >= 0 ? 'heat-positive' : 'heat-negative';
        return (
          <div
            className={`heat-cell ${colorClass}`}
            key={position.ticker}
            style={{ flexBasis: `${weight}%`, flexGrow: weight }}
            title={`${position.ticker} ${currencyFormatter.format(position.market_value)}`}
          >
            <strong>{position.ticker}</strong>
            <span>{currencyFormatter.format(position.market_value)}</span>
            <small>{position.change_percent.toFixed(2)}%</small>
          </div>
        );
      })}
    </div>
  );
}

function MiniMetric({ label, value, className = '' }: { label: string; value: string; className?: string }) {
  const testId = `metric-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
  return (
    <div className={`metric ${className}`} data-testid={testId}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default function TradingWorkstation() {
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>(FALLBACK_WATCHLIST);
  const [portfolio, setPortfolio] = useState<Portfolio>(EMPTY_PORTFOLIO);
  const [history, setHistory] = useState<HistorySnapshot[]>([]);
  const [series, setSeries] = useState<SparkSeries>(() =>
    Object.fromEntries(FALLBACK_WATCHLIST.map((item) => [item.ticker, [item.previous_price ?? item.current_price, item.current_price]]))
  );
  const [selectedTicker, setSelectedTicker] = useState('AAPL');
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>('disconnected');
  const [flash, setFlash] = useState<FlashMap>({});
  const [tradeTicker, setTradeTicker] = useState('AAPL');
  const [tradeQuantity, setTradeQuantity] = useState('1');
  const [watchlistTicker, setWatchlistTicker] = useState('');
  const [chatInput, setChatInput] = useState('');
  const [chatLoading, setChatLoading] = useState(false);
  const [notice, setNotice] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      role: 'assistant',
      content: 'I can analyze this simulated portfolio, place guarded trades, and manage the watchlist.'
    }
  ]);
  const flashTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  const refreshPortfolio = useCallback(async () => {
    const payload = await getJson<Portfolio>('/api/portfolio', EMPTY_PORTFOLIO);
    setPortfolio({
      cash_balance: toNumber(payload.cash_balance, EMPTY_PORTFOLIO.cash_balance),
      total_value: toNumber(payload.total_value, EMPTY_PORTFOLIO.total_value),
      unrealized_pnl: toNumber(payload.unrealized_pnl),
      positions: Array.isArray(payload.positions) ? payload.positions : []
    });
  }, []);

  const refreshHistory = useCallback(async () => {
    const payload = await getJson<{ snapshots?: HistorySnapshot[] }>('/api/portfolio/history', { snapshots: [] });
    setHistory(Array.isArray(payload.snapshots) ? payload.snapshots : []);
  }, []);

  const refreshWatchlist = useCallback(async () => {
    const payload = await getJson<{ items?: WatchlistItem[] }>('/api/watchlist', { items: FALLBACK_WATCHLIST });
    const items = Array.isArray(payload.items) && payload.items.length ? payload.items : FALLBACK_WATCHLIST;
    setWatchlist(items);
    setSelectedTicker((current) => (items.some((item) => item.ticker === current) ? current : items[0]?.ticker ?? current));
    setTradeTicker((current) => (items.some((item) => item.ticker === current) ? current : items[0]?.ticker ?? current));
    setSeries((current) => {
      const next = { ...current };
      items.forEach((item) => {
        if (!next[item.ticker]) next[item.ticker] = [item.previous_price ?? item.current_price, item.current_price];
      });
      return next;
    });
  }, []);

  useEffect(() => {
    refreshWatchlist();
    refreshPortfolio();
    refreshHistory();
  }, [refreshHistory, refreshPortfolio, refreshWatchlist]);

  useEffect(() => {
    if (typeof EventSource === 'undefined') return;
    let closed = false;
    const source = new EventSource('/api/stream/prices');
    setConnectionStatus('reconnecting');

    source.onopen = () => setConnectionStatus('connected');
    source.onerror = () => {
      if (!closed) setConnectionStatus('reconnecting');
    };
    source.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as Partial<WatchlistItem> & { price?: number };
        const ticker = normalizeTicker(String(data.ticker ?? ''));
        const currentPrice = toNumber(data.current_price ?? data.price);
        if (!ticker || currentPrice <= 0) return;

        setWatchlist((items) => {
          const existing = items.find((item) => item.ticker === ticker);
          const previousPrice = toNumber(data.previous_price, existing?.current_price ?? currentPrice);
          const direction = (data.direction as Direction | undefined) ?? directionFromPrices(currentPrice, previousPrice);
          const changePercent = toNumber(data.change_percent, existing?.change_percent ?? 0);

          const nextItem: WatchlistItem = {
            ticker,
            current_price: currentPrice,
            previous_price: previousPrice,
            change_percent: changePercent,
            direction,
            timestamp: data.timestamp ?? new Date().toISOString()
          };

          if (!existing) return [...items, nextItem];
          return items.map((item) => (item.ticker === ticker ? { ...item, ...nextItem } : item));
        });

        setSeries((current) => ({ ...current, [ticker]: mergeSeries(current[ticker] ?? [], currentPrice) }));
        setFlash((current) => ({ ...current, [ticker]: directionFromPrices(currentPrice, data.previous_price) }));
        clearTimeout(flashTimers.current[ticker]);
        flashTimers.current[ticker] = setTimeout(() => {
          setFlash((current) => {
            const next = { ...current };
            delete next[ticker];
            return next;
          });
        }, 540);
      } catch {
        setConnectionStatus('reconnecting');
      }
    };

    return () => {
      closed = true;
      source.close();
      setConnectionStatus('disconnected');
      Object.values(flashTimers.current).forEach(clearTimeout);
    };
  }, []);

  const selectedSeries = series[selectedTicker] ?? [];
  const selectedItem = watchlist.find((item) => item.ticker === selectedTicker);
  const historyValues = history.map((snapshot) => snapshot.total_value);
  const historyLabels = history.map((snapshot) => new Date(snapshot.recorded_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));

  const executeTrade = async (side: Side) => {
    const ticker = normalizeTicker(tradeTicker);
    const quantity = Number(tradeQuantity);
    if (!ticker || !Number.isFinite(quantity) || quantity <= 0) {
      setNotice('Enter a ticker and a positive quantity.');
      return;
    }

    try {
      const payload = await sendJson<{ portfolio?: Portfolio }>('/api/portfolio/trade', { ticker, quantity, side });
      if (payload.portfolio) setPortfolio(payload.portfolio);
      setNotice(`${side.toUpperCase()} ${numberFormatter.format(quantity)} ${ticker} submitted.`);
      refreshHistory();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Trade failed.');
    }
  };

  const addWatchlistTicker = async (event: FormEvent) => {
    event.preventDefault();
    const ticker = normalizeTicker(watchlistTicker);
    if (!ticker) return;
    try {
      const payload = await sendJson<{ item?: WatchlistItem; already_exists?: boolean }>('/api/watchlist', { ticker });
      if (payload.item) {
        setWatchlist((items) => (items.some((item) => item.ticker === payload.item?.ticker) ? items : [...items, payload.item as WatchlistItem]));
        setSeries((current) => ({ ...current, [ticker]: current[ticker] ?? [payload.item?.current_price ?? 0] }));
      } else {
        await refreshWatchlist();
      }
      setWatchlistTicker('');
      setNotice(payload.already_exists ? `${ticker} is already on the watchlist.` : `${ticker} added to the watchlist.`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : `Could not add ${ticker}.`);
    }
  };

  const removeWatchlistTicker = async (ticker: string) => {
    try {
      await sendJson(`/api/watchlist/${encodeURIComponent(ticker)}`, {}, 'DELETE');
      setWatchlist((items) => items.filter((item) => item.ticker !== ticker));
      if (selectedTicker === ticker) setSelectedTicker(watchlist.find((item) => item.ticker !== ticker)?.ticker ?? 'AAPL');
      setNotice(`${ticker} removed from the watchlist.`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : `Could not remove ${ticker}.`);
    }
  };

  const submitChat = async (event: FormEvent) => {
    event.preventDefault();
    const message = chatInput.trim();
    if (!message) return;
    setChatInput('');
    setChatLoading(true);
    setMessages((current) => [...current, { role: 'user', content: message }]);
    try {
      const response = await sendJson<{
        message?: string;
        trades?: unknown[];
        watchlist_changes?: unknown[];
        errors?: ChatActionError[];
      }>('/api/chat', { message });
      const details = [
        response.trades?.length ? `${response.trades.length} trade action(s)` : '',
        response.watchlist_changes?.length ? `${response.watchlist_changes.length} watchlist change(s)` : '',
        ...(response.errors ?? []).map(formatChatError)
      ].filter(Boolean);
      setMessages((current) => [
        ...current,
        {
          role: 'assistant',
          content: response.message ?? 'Done.',
          details
        }
      ]);
      refreshPortfolio();
      refreshHistory();
      refreshWatchlist();
    } catch (error) {
      setMessages((current) => [
        ...current,
        {
          role: 'system',
          content: error instanceof Error ? error.message : 'Chat request failed.'
        }
      ]);
    } finally {
      setChatLoading(false);
    }
  };

  return (
    <main className="workstation">
      <header className="topbar">
        <div>
          <p className="eyebrow">FinAlly</p>
          <h1>Trading Workstation</h1>
        </div>
        <div className="top-metrics">
          <MiniMetric label="Total Value" value={currencyFormatter.format(portfolio.total_value)} />
          <MiniMetric label="Cash" value={currencyFormatter.format(portfolio.cash_balance)} />
          <MiniMetric label="Unrealized P&L" value={currencyFormatter.format(portfolio.unrealized_pnl)} className={percentClass(portfolio.unrealized_pnl)} />
          <div className="connection">
            <span className={`status-dot ${connectionStatus}`} />
            <strong>{connectionStatus}</strong>
          </div>
        </div>
      </header>

      <section className="terminal-grid">
        <aside className="panel watchlist-panel">
          <div className="panel-heading">
            <div>
              <span>Live Watchlist</span>
              <strong>{watchlist.length} symbols</strong>
            </div>
          </div>
          <form className="add-form" onSubmit={addWatchlistTicker}>
            <input value={watchlistTicker} onChange={(event) => setWatchlistTicker(normalizeTicker(event.target.value))} placeholder="Ticker" aria-label="Add ticker" />
            <button type="submit">Add</button>
          </form>
          <div className="watchlist-table" data-testid="watchlist-table">
            {watchlist.map((item) => {
              const itemDirection = flash[item.ticker] ?? item.direction ?? 'flat';
              const isSelected = selectedTicker === item.ticker;
              return (
                <button
                  className={`watch-row ${isSelected ? 'selected' : ''} ${flash[item.ticker] ? `flash-${itemDirection}` : ''}`}
                  data-testid={`watch-row-${item.ticker}`}
                  key={item.ticker}
                  onClick={() => {
                    setSelectedTicker(item.ticker);
                    setTradeTicker(item.ticker);
                  }}
                  type="button"
                >
                  <span className="ticker-cell">
                    <strong>{item.ticker}</strong>
                    <small>{itemDirection.toUpperCase()}</small>
                  </span>
                  <span className="price-cell">{currencyFormatter.format(item.current_price)}</span>
                  <span className={`change-cell ${percentClass(item.change_percent)}`}>{toNumber(item.change_percent).toFixed(2)}%</span>
                  <Sparkline values={series[item.ticker] ?? [item.current_price]} stroke={itemDirection === 'down' ? '#f0626e' : '#37c978'} />
                  <span
                    className="remove-watch"
                    onClick={(event) => {
                      event.stopPropagation();
                      removeWatchlistTicker(item.ticker);
                    }}
                    role="button"
                    aria-label={`Remove ${item.ticker}`}
                    tabIndex={0}
                  >
                    x
                  </span>
                </button>
              );
            })}
          </div>
        </aside>

        <section className="main-stack">
          <section className="panel chart-panel">
            <div className="panel-heading">
              <div>
                <span>Selected Ticker</span>
                <strong>{selectedTicker}</strong>
              </div>
              <div className="chart-price">
                {selectedItem ? currencyFormatter.format(selectedItem.current_price) : '--'}
                <small className={percentClass(selectedItem?.change_percent)}>{toNumber(selectedItem?.change_percent).toFixed(2)}%</small>
              </div>
            </div>
            <LineChart values={selectedSeries} emptyLabel="Waiting for live ticks" />
          </section>

          <section className="panel trade-panel">
            <div className="trade-controls">
              <label>
                <span>Ticker</span>
                <input aria-label="Trade ticker" value={tradeTicker} onChange={(event) => setTradeTicker(normalizeTicker(event.target.value))} />
              </label>
              <label>
                <span>Quantity</span>
                <input aria-label="Trade quantity" inputMode="decimal" min="0" step="0.0001" type="number" value={tradeQuantity} onChange={(event) => setTradeQuantity(event.target.value)} />
              </label>
              <button className="buy" type="button" onClick={() => executeTrade('buy')}>
                Buy
              </button>
              <button className="sell" type="button" onClick={() => executeTrade('sell')}>
                Sell
              </button>
            </div>
            {notice ? (
              <p className="notice" data-testid="notice" role="status">
                {notice}
              </p>
            ) : null}
          </section>

          <section className="portfolio-grid">
            <div className="panel">
              <div className="panel-heading">
                <span>Portfolio Heatmap</span>
              </div>
              <PortfolioHeatmap positions={portfolio.positions} totalValue={portfolio.total_value} />
            </div>
            <div className="panel">
              <div className="panel-heading">
                <span>P&L History</span>
              </div>
              <LineChart values={historyValues} labels={historyLabels} tone="yellow" emptyLabel="No snapshots yet" />
            </div>
          </section>

          <section className="panel positions-panel">
            <div className="panel-heading">
              <span>Positions</span>
            </div>
            <div className="positions-table">
              <div className="table-header">
                <span>Ticker</span>
                <span>Qty</span>
                <span>Avg Cost</span>
                <span>Last</span>
                <span>Market Value</span>
                <span>Unrealized P&L</span>
                <span>%</span>
              </div>
              {portfolio.positions.length ? (
                portfolio.positions.map((position) => (
                  <div className="table-row" data-testid={`position-row-${position.ticker}`} key={position.ticker}>
                    <strong>{position.ticker}</strong>
                    <span>{numberFormatter.format(position.quantity)}</span>
                    <span>{currencyFormatter.format(position.avg_cost)}</span>
                    <span>{currencyFormatter.format(position.current_price)}</span>
                    <span>{currencyFormatter.format(position.market_value)}</span>
                    <span className={percentClass(position.unrealized_pnl)}>{currencyFormatter.format(position.unrealized_pnl)}</span>
                    <span className={percentClass(position.change_percent)}>{position.change_percent.toFixed(2)}%</span>
                  </div>
                ))
              ) : (
                <div className="table-empty">No open positions</div>
              )}
            </div>
          </section>
        </section>

        <aside className="panel chat-panel">
          <div className="panel-heading">
            <div>
              <span>AI Assistant</span>
              <strong>Portfolio copilot</strong>
            </div>
          </div>
          <div className="chat-log" data-testid="chat-log">
            {messages.map((message, index) => (
              <article className={`chat-message ${message.role}`} key={`${message.role}-${index}`}>
                <p>{message.content}</p>
                {message.details?.length ? (
                  <ul>
                    {message.details.map((detail) => (
                      <li key={detail}>{detail}</li>
                    ))}
                  </ul>
                ) : null}
              </article>
            ))}
            {chatLoading ? <article className="chat-message assistant loading">Thinking...</article> : null}
          </div>
          <form className="chat-form" onSubmit={submitChat}>
            <textarea aria-label="Assistant message" value={chatInput} onChange={(event) => setChatInput(event.target.value)} placeholder="Ask for analysis or an action..." />
            <button type="submit" disabled={chatLoading}>
              Send
            </button>
          </form>
        </aside>
      </section>
    </main>
  );
}
