/**
 * WebSocket client for live portfolio updates.
 */

type MessageHandler = (data: PortfolioUpdate) => void;

export interface Greeks {
	delta: number;
	gamma: number;
	theta: number;
	vega: number;
	vanna: number;
	charm: number;
	vomma: number;
	implied_volatility: number;
}

export interface PositionData {
	symbol: string;
	underlying: string;
	contract_type: 'call' | 'put' | 'stock';
	strike: number;
	expiration: string;
	quantity: number;
	cost_basis: number;
	multiplier: number;
	underlying_price: number;
	option_mid: number | null;
	greeks: Greeks;
}

export interface Advice {
	level: 'info' | 'warning' | 'urgent';
	message: string;
	position_symbol: string;
	metric: string;
	value: number;
	threshold: number;
}

export interface UnderlyingSummary {
	net_delta: number;
	net_gamma: number;
	net_theta: number;
	net_vega: number;
	beta: number;
	beta_weighted_delta: number;
	positions: PositionData[];
	advice: Advice[];
}

export interface PortfolioRollup {
	net_delta: number;
	net_gamma: number;
	net_theta: number;
	net_vega: number;
	beta_weighted_delta: number;
	spy_price: number;
}

export interface GEXStrike {
	strike: number;
	call_gex: number;
	put_gex: number;
	net_gex: number;
	call_oi: number;
	put_oi: number;
}

export interface GEXProfile {
	underlying: string;
	spot_price: number;
	net_gex: number;
	call_wall: number;
	put_wall: number;
	flip_point: number;
	expirations: string[];
	fetched_at: string;
	strikes: GEXStrike[];
}

export interface PortfolioUpdate {
	type: string;
	last_updated: string;
	market_open: boolean;
	portfolio: PortfolioRollup;
	underlyings: Record<string, UnderlyingSummary>;
	gex?: Record<string, GEXProfile>;
}

export class PortfolioWebSocket {
	private ws: WebSocket | null = null;
	private handlers: Set<MessageHandler> = new Set();
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	private _connected = false;

	get connected(): boolean {
		return this._connected;
	}

	connect(): void {
		if (this.ws) return;

		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const url = `${protocol}//${window.location.host}/api/ws`;

		this.ws = new WebSocket(url);

		this.ws.onopen = () => {
			this._connected = true;
			console.log('WebSocket connected');
		};

		this.ws.onmessage = (event) => {
			try {
				const data: PortfolioUpdate = JSON.parse(event.data);
				for (const handler of this.handlers) {
					handler(data);
				}
			} catch (e) {
				console.error('Failed to parse WebSocket message:', e);
			}
		};

		this.ws.onclose = () => {
			this._connected = false;
			this.ws = null;
			console.log('WebSocket disconnected, reconnecting in 3s...');
			this.reconnectTimer = setTimeout(() => this.connect(), 3000);
		};

		this.ws.onerror = (error) => {
			console.error('WebSocket error:', error);
		};
	}

	disconnect(): void {
		if (this.reconnectTimer) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}
		if (this.ws) {
			this.ws.close();
			this.ws = null;
		}
		this._connected = false;
	}

	onMessage(handler: MessageHandler): () => void {
		this.handlers.add(handler);
		return () => this.handlers.delete(handler);
	}

	requestRefresh(): void {
		if (this.ws?.readyState === WebSocket.OPEN) {
			this.ws.send(JSON.stringify({ type: 'refresh' }));
		}
	}

	requestGexRefresh(): void {
		if (this.ws?.readyState === WebSocket.OPEN) {
			this.ws.send(JSON.stringify({ type: 'gex_refresh' }));
		}
	}
}
