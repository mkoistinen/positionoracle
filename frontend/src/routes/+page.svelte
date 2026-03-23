<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { getAuthStatus, importPositions, fetchPositionsFromIB, analyzeSymbol, logout } from '$lib/api';
	import { PortfolioWebSocket, type PortfolioUpdate, type PortfolioRollup, type UnderlyingSummary } from '$lib/ws';
	import { evaluateAll, evaluateNetDelta, evaluateNetTheta, evaluateNetVega, evaluateNetGamma, evaluateBetaWeightedDelta, signalClass } from '$lib/greek-signals';
	import { tooltip } from '$lib/tooltip';
	import { marked } from 'marked';

	let authenticated = $state(false);
	let hasCredentials = $state(false);
	let loading = $state(true);
	let underlyings = $state<Record<string, UnderlyingSummary>>({});
	let connected = $state(false);
	let importMessage = $state('');
	let lastUpdated = $state('');
	let marketOpen = $state(false);
	let portfolio = $state<PortfolioRollup>({ net_delta: 0, net_gamma: 0, net_theta: 0, net_vega: 0 });
	let analyses = $state<Record<string, string>>({});
	let analyzing = $state<Record<string, boolean>>({});
	let analysisVisible = $state<Record<string, boolean>>({});

	async function handleClaude(ticker: string, event: MouseEvent) {
		event.stopPropagation();

		const hasAnalysis = !!analyses[ticker];
		const forceRefresh = event.shiftKey;

		if (hasAnalysis && !forceRefresh) {
			// Toggle visibility
			analysisVisible[ticker] = !analysisVisible[ticker];
			return;
		}

		// Query or re-query
		analyzing[ticker] = true;
		analysisVisible[ticker] = true;
		try {
			const result = await analyzeSymbol(ticker);
			analyses[ticker] = result.analysis;
		} catch (e) {
			analyses[ticker] = `Analysis failed: ${e}`;
		} finally {
			analyzing[ticker] = false;
		}
	}

	const EXPANDED_KEY = 'po_expanded';

	function loadExpanded(): Record<string, boolean> {
		try {
			const raw = localStorage.getItem(EXPANDED_KEY);
			return raw ? JSON.parse(raw) : {};
		} catch {
			return {};
		}
	}

	function saveExpanded() {
		localStorage.setItem(EXPANDED_KEY, JSON.stringify(expanded));
	}

	let expanded = $state<Record<string, boolean>>(loadExpanded());

	function toggleExpanded(ticker: string, event: MouseEvent) {
		if (event.altKey) {
			const allValue = !expanded[ticker];
			for (const key of Object.keys(underlyings)) {
				expanded[key] = allValue;
			}
		} else if (event.shiftKey && !expanded[ticker]) {
			for (const key of Object.keys(underlyings)) {
				expanded[key] = key === ticker;
			}
		} else {
			expanded[ticker] = !expanded[ticker];
		}
		saveExpanded();
	}

	let ws: PortfolioWebSocket | null = null;

	onMount(async () => {
		try {
			const status = await getAuthStatus();
			authenticated = status.authenticated;
			hasCredentials = status.has_credentials;

			if (authenticated) {
				startWebSocket();
				handleFetchFromIB(false);
			}
		} catch (e) {
			console.error('Failed to check auth status:', e);
		} finally {
			loading = false;
		}
	});

	onDestroy(() => {
		ws?.disconnect();
	});

	function startWebSocket() {
		ws = new PortfolioWebSocket();
		ws.onMessage((data: PortfolioUpdate) => {
			if (data.type === 'update') {
				underlyings = data.underlyings;
				lastUpdated = data.last_updated;
				marketOpen = data.market_open;
				portfolio = data.portfolio;
			}
		});
		ws.connect();

		const interval = setInterval(() => {
			connected = ws?.connected ?? false;
		}, 1000);

		return () => clearInterval(interval);
	}

	async function handleLogout() {
		await logout();
		ws?.disconnect();
		authenticated = false;
		underlyings = {};
	}

	async function handleImport(event: Event) {
		const input = event.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;

		try {
			const result = await importPositions(file);
			importMessage = `Imported ${result.imported} positions`;
			ws?.requestRefresh();
		} catch (e) {
			importMessage = `Import failed: ${e}`;
		}

		input.value = '';
	}

	let fetching = $state(false);

	async function handleFetchFromIB(force: boolean = true) {
		fetching = true;
		importMessage = '';
		try {
			const result = await fetchPositionsFromIB(force);
			const label = result.cached ? 'Loaded' : 'Fetched';
			importMessage = `${label} ${result.imported} positions from IB`;
			ws?.requestRefresh();
		} catch (e) {
			importMessage = `Fetch failed: ${e}`;
		} finally {
			fetching = false;
		}
	}

	function formatGreek(value: number, decimals: number = 4): string {
		return value.toFixed(decimals);
	}

	function formatPrice(value: number): string {
		return value.toFixed(2);
	}

	function adviceLevelClass(level: string): string {
		switch (level) {
			case 'urgent': return 'advice-urgent';
			case 'warning': return 'advice-warning';
			default: return 'advice-info';
		}
	}

	function formatTimestamp(iso: string): string {
		if (!iso) return '';
		const d = new Date(iso);
		return d.toLocaleTimeString('en-US', {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit',
			timeZoneName: 'short',
		});
	}
</script>

{#if loading}
	<div class="center">Loading...</div>
{:else if !authenticated}
	<div class="center">
		<div class="login-card">
			<h1>PositionOracle</h1>
			<p>Options position monitor with real-time Greeks</p>
			{#if !hasCredentials}
				<p class="muted">No passkeys registered. Use setup token to register.</p>
				<a href="/login" class="btn">Set Up Passkey</a>
			{:else}
				<a href="/login" class="btn">Sign In with Passkey</a>
			{/if}
		</div>
	</div>
{:else}
	<header>
		<div class="header-left">
			<h1>PositionOracle</h1>
			<span class="status" class:status-connected={connected} class:status-disconnected={!connected}>
				{connected ? 'Live' : 'Disconnected'}
			</span>
			<span class="market-status" class:market-open={marketOpen} class:market-closed={!marketOpen}>
				{marketOpen ? 'Market Open' : 'Market Closed'}
			</span>
			{#if lastUpdated}
				<span class="last-updated">{formatTimestamp(lastUpdated)}</span>
			{/if}
		</div>
		<div class="header-right">
			<button class="btn btn-secondary" onclick={handleFetchFromIB} disabled={fetching}>
				{fetching ? 'Fetching...' : 'Fetch from IB'}
			</button>
			<button class="btn btn-ghost" onclick={handleLogout}>Logout</button>
		</div>
	</header>

	{#if importMessage}
		<div class="import-message">
			{importMessage}
			<button class="import-message-close" onclick={() => importMessage = ''}>&times;</button>
		</div>
	{/if}

	<main>
		{#if Object.keys(underlyings).length === 0}
			<div class="empty">
				<p>No positions loaded. Import a Flex Query XML to get started.</p>
			</div>
		{:else}
			{@const pt = evaluateNetTheta(portfolio.net_theta)}
			{@const pv = evaluateNetVega(portfolio.net_vega)}
			{@const pg_ = evaluateNetGamma(portfolio.net_gamma)}
			{@const pbw = evaluateBetaWeightedDelta(portfolio.beta_weighted_delta)}
			<div class="portfolio-bar">
				<span class="portfolio-label">Portfolio</span>
				<div class="net-greeks">
					<span class="greek-badge {signalClass(pbw.level)}" use:tooltip={pbw.reason}>
						SPY &Delta; {formatGreek(portfolio.beta_weighted_delta, 2)}
					</span>
					<span class="greek-badge {signalClass(pt.level)}" use:tooltip={pt.reason}>
						&Theta; {formatGreek(portfolio.net_theta, 2)}
					</span>
					<span class="greek-badge {signalClass(pv.level)}" use:tooltip={pv.reason}>
						V {formatGreek(portfolio.net_vega, 2)}
					</span>
					<span class="greek-badge {signalClass(pg_.level)}" use:tooltip={pg_.reason}>
						&Gamma; {formatGreek(portfolio.net_gamma, 2)}
					</span>
				</div>
			</div>

			{#each Object.entries(underlyings) as [ticker, summary]}
				{@const nd = evaluateNetDelta(summary.net_delta)}
				{@const nbw = evaluateBetaWeightedDelta(summary.beta_weighted_delta, summary.beta)}
				{@const nt = evaluateNetTheta(summary.net_theta)}
				{@const nv = evaluateNetVega(summary.net_vega)}
				{@const ng = evaluateNetGamma(summary.net_gamma)}
				<section class="underlying-card">
					<div
						class="underlying-header"
						role="button"
						tabindex="0"
						onclick={(e: MouseEvent) => toggleExpanded(ticker, e)}
						onkeydown={(e: KeyboardEvent) => { if (e.key === 'Enter') toggleExpanded(ticker, e as unknown as MouseEvent); }}
					>
						<div class="underlying-title">
							<span class="caret" class:caret-open={expanded[ticker]}>&#9654;</span>
							<h2>{ticker}</h2>
							<span class="spot-price">
								{summary.positions[0]?.underlying_price ? formatPrice(summary.positions[0].underlying_price) : '—'}
							</span>
						</div>
						<div class="net-greeks">
							<span class="greek-badge {signalClass(nd.level)}" use:tooltip={nd.reason}>
								&Delta; {formatGreek(summary.net_delta, 2)}
							</span>
							<span class="greek-badge {signalClass(nbw.level)}" use:tooltip={nbw.reason}>
								SPY &Delta; {formatGreek(summary.beta_weighted_delta, 2)}
							</span>
							<span class="greek-badge {signalClass(nt.level)}" use:tooltip={nt.reason}>
								&Theta; {formatGreek(summary.net_theta, 2)}
							</span>
							<span class="greek-badge {signalClass(nv.level)}" use:tooltip={nv.reason}>
								V {formatGreek(summary.net_vega, 2)}
							</span>
							<span class="greek-badge {signalClass(ng.level)}" use:tooltip={ng.reason}>
								&Gamma; {formatGreek(summary.net_gamma, 2)}
							</span>
							<button
								class="claude-btn"
								class:claude-active={analysisVisible[ticker]}
								class:claude-spinning={analyzing[ticker]}
								onclick={(e: MouseEvent) => handleClaude(ticker, e)}
								use:tooltip={analyses[ticker] ? 'Click to toggle. Shift-click to refresh.' : 'Ask Claude for analysis'}
							>
								✦
							</button>
						</div>
					</div>

					{#if expanded[ticker]}
					{#if summary.advice.length > 0}
						<div class="advice-list">
							{#each summary.advice as item}
								<div class="advice-item {adviceLevelClass(item.level)}">
									<span class="advice-level">{item.level.toUpperCase()}</span>
									{item.message}
								</div>
							{/each}
						</div>
					{/if}

					<div class="table-wrapper">
						<table>
							<thead>
								<tr>
									<th>Type</th>
									<th>Strike</th>
									<th>Exp</th>
									<th>Qty</th>
									<th>Mid</th>
									<th>IV</th>
									<th>&Delta;</th>
									<th>&Theta;</th>
									<th>Vega</th>
									<th>&Gamma;</th>
									<th>Vanna</th>
									<th>Charm</th>
									<th>Vomma</th>
								</tr>
							</thead>
							<tbody>
								{#each summary.positions as pos}
									{@const isStock = pos.contract_type === 'stock'}
									{@const signals = evaluateAll(pos)}
									<tr>
										<td class="type-{pos.contract_type}">{pos.contract_type.toUpperCase()}</td>
										<td>{isStock ? '—' : formatPrice(pos.strike)}</td>
										<td>{isStock ? '—' : pos.expiration}</td>
										<td class:negative={pos.quantity < 0}>{pos.quantity}</td>
										<td>{isStock ? '—' : (pos.option_mid != null ? formatPrice(pos.option_mid) : '—')}</td>
										<td>{isStock ? '—' : (pos.greeks.implied_volatility * 100).toFixed(1) + '%'}</td>
										<td class={signalClass(signals.delta.level)} use:tooltip={signals.delta.reason}>
											{isStock ? pos.quantity : formatGreek(pos.greeks.delta)}
										</td>
										<td class={signalClass(signals.theta.level)} use:tooltip={signals.theta.reason}>
											{isStock ? '—' : formatGreek(pos.greeks.theta)}
										</td>
										<td class={signalClass(signals.vega.level)} use:tooltip={signals.vega.reason}>
											{isStock ? '—' : formatGreek(pos.greeks.vega)}
										</td>
										<td class={signalClass(signals.gamma.level)} use:tooltip={signals.gamma.reason}>
											{isStock ? '—' : formatGreek(pos.greeks.gamma)}
										</td>
										<td class={signalClass(signals.vanna.level)} use:tooltip={signals.vanna.reason}>
											{isStock ? '—' : formatGreek(pos.greeks.vanna)}
										</td>
										<td class={signalClass(signals.charm.level)} use:tooltip={signals.charm.reason}>
											{isStock ? '—' : formatGreek(pos.greeks.charm)}
										</td>
										<td class={signalClass(signals.vomma.level)} use:tooltip={signals.vomma.reason}>
											{isStock ? '—' : formatGreek(pos.greeks.vomma)}
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					</div>

					<div class="analysis-section">
						{#if analyses[ticker] && analysisVisible[ticker]}
							<div class="analysis-content">
								{@html marked(analyses[ticker])}
							</div>
						{/if}
					</div>
					{/if}
				</section>
			{/each}
		{/if}
	</main>
{/if}

<style>
	.center {
		display: flex;
		align-items: center;
		justify-content: center;
		min-height: 100vh;
	}

	.login-card {
		text-align: center;
		padding: 3rem;
		background: #1e293b;
		border-radius: 12px;
		max-width: 400px;
	}

	.login-card h1 {
		margin: 0 0 0.5rem;
		font-size: 2rem;
	}

	.muted {
		color: #94a3b8;
		font-size: 0.875rem;
	}

	.btn {
		display: inline-block;
		padding: 0.75rem 1.5rem;
		background: #3b82f6;
		color: white;
		border: none;
		border-radius: 8px;
		cursor: pointer;
		font-size: 1rem;
		text-decoration: none;
		transition: background 0.2s;
	}

	.btn:hover {
		background: #2563eb;
	}

	.btn-secondary {
		background: #475569;
	}

	.btn-secondary:hover {
		background: #64748b;
	}

	.btn-ghost {
		background: transparent;
		color: #94a3b8;
		padding: 0.5rem 1rem;
	}

	.btn-ghost:hover {
		color: #e2e8f0;
	}

	header {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		align-items: center;
		padding: 1rem 2rem;
		gap: 0.5rem;
		background: #1e293b;
		border-bottom: 1px solid #334155;
	}

	.header-left {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem;
	}

	.header-right {
		display: flex;
		gap: 0.5rem;
		align-items: center;
		flex-shrink: 0;
	}

	.header-left h1 {
		margin: 0;
		font-size: 1.25rem;
	}


	.status {
		font-size: 0.75rem;
		padding: 0.25rem 0.75rem;
		border-radius: 999px;
		font-weight: 600;
	}

	.status-connected {
		background: #065f46;
		color: #6ee7b7;
	}

	.status-disconnected {
		background: #7f1d1d;
		color: #fca5a5;
	}

	.market-status {
		font-size: 0.75rem;
		padding: 0.25rem 0.75rem;
		border-radius: 999px;
		font-weight: 600;
	}

	.market-open {
		background: #065f46;
		color: #6ee7b7;
	}

	.market-closed {
		background: #78350f;
		color: #fbbf24;
	}

	.last-updated {
		font-size: 0.75rem;
		color: #64748b;
		font-family: 'SF Mono', 'Fira Code', monospace;
	}

	.import-message {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 0.75rem 2rem;
		background: #1e3a5f;
		color: #93c5fd;
		font-size: 0.875rem;
	}

	.import-message-close {
		background: #2b4a6e;
		border: 1px solid #3b6998;
		border-radius: 50%;
		color: #93c5fd;
		font-size: 0.875rem;
		width: 1.5rem;
		height: 1.5rem;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
		line-height: 1;
		flex-shrink: 0;
		transition: background 0.2s, border-color 0.2s;
	}

	.import-message-close:hover {
		background: #3b6998;
		border-color: #93c5fd;
	}

	main {
		padding: 1.5rem 2rem;
		display: flex;
		flex-direction: column;
		gap: 1.5rem;
	}

	.empty {
		text-align: center;
		padding: 4rem;
		color: #94a3b8;
	}

	.portfolio-bar {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		align-items: center;
		padding: 1rem 1.5rem;
		gap: 0.5rem;
		background: #1e293b;
		border-radius: 12px;
		border: 1px solid #475569;
	}

	.portfolio-label {
		font-size: 1.1rem;
		flex-shrink: 0;
		font-weight: 700;
	}

	.underlying-card {
		background: #1e293b;
		border-radius: 12px;
		overflow: hidden;
	}

	.underlying-header {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		align-items: center;
		padding: 1rem 1.5rem;
		gap: 0.5rem;
		border-bottom: 1px solid #334155;
		width: 100%;
		background: none;
		border-top: none;
		border-left: none;
		border-right: none;
		color: inherit;
		font: inherit;
		cursor: pointer;
		text-align: left;
	}

	.underlying-header:hover {
		background: #253347;
	}

	.caret {
		font-size: 1.125rem;
		color: #64748b;
		transition: transform 0.2s ease;
		display: inline-flex;
		align-items: center;
		line-height: 1;
	}

	.caret-open {
		transform: rotate(90deg);
	}

	.underlying-title {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		flex-shrink: 0;
	}

	.underlying-header h2 {
		margin: 0;
		font-size: 1.25rem;
	}

	.spot-price {
		font-size: 1.1rem;
		font-family: 'SF Mono', 'Fira Code', monospace;
		color: #94a3b8;
	}

	.net-greeks {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	.greek-badge {
		background: #334155;
		padding: 0.25rem 0.75rem;
		border-radius: 6px;
		font-size: 0.8125rem;
		font-family: 'SF Mono', 'Fira Code', monospace;
	}

	.advice-list {
		padding: 0.75rem 1.5rem;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.advice-item {
		padding: 0.5rem 1rem;
		border-radius: 6px;
		font-size: 0.875rem;
	}

	.advice-urgent {
		background: #7f1d1d;
		border-left: 3px solid #ef4444;
	}

	.advice-warning {
		background: #78350f;
		border-left: 3px solid #f59e0b;
	}

	.advice-info {
		background: #1e3a5f;
		border-left: 3px solid #3b82f6;
	}

	.advice-level {
		font-weight: 700;
		margin-right: 0.5rem;
		font-size: 0.75rem;
	}

	.table-wrapper {
		overflow-x: auto;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.8125rem;
		font-family: 'SF Mono', 'Fira Code', monospace;
	}

	th {
		text-align: right;
		padding: 0.5rem 0.75rem;
		color: #94a3b8;
		font-weight: 600;
		border-bottom: 1px solid #334155;
		white-space: nowrap;
	}

	th:first-child,
	td:first-child {
		text-align: left;
	}

	td {
		text-align: right;
		padding: 0.5rem 0.75rem;
		border-bottom: 1px solid #1e293b;
		white-space: nowrap;
		cursor: default;
	}

	tr:hover td {
		background: #334155;
	}

	.type-call {
		color: #6ee7b7;
	}

	.type-put {
		color: #fca5a5;
	}

	.type-stock {
		color: #c4b5fd;
	}

	.claude-btn {
		background: none;
		border: none;
		cursor: pointer;
		font-size: 1.25rem;
		color: #92400e;
		padding: 0;
		line-height: 1;
		transition: color 0.2s, transform 0.3s;
		display: inline-flex;
		align-items: center;
		outline: none;
		-webkit-user-select: none;
		user-select: none;
	}

	.claude-btn:hover {
		color: #f59e0b;
	}

	.claude-active {
		color: #f59e0b;
	}

	.claude-spinning {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	.analysis-section {
		padding: 0 1.5rem 1rem;
	}

	.analysis-content {
		margin-top: 1rem;
		padding: 1rem;
		background: #334155;
		border-radius: 8px;
		font-size: 0.875rem;
		line-height: 1.7;
		color: #e2e8f0;
	}

	.analysis-content :global(strong) {
		color: #f8fafc;
	}

	.analysis-content :global(h1),
	.analysis-content :global(h2),
	.analysis-content :global(h3) {
		color: #f8fafc;
		margin: 1rem 0 0.5rem;
		font-size: 1rem;
	}

	.analysis-content :global(h1) {
		font-size: 1.125rem;
	}

	.analysis-content :global(ul),
	.analysis-content :global(ol) {
		padding-left: 1.5rem;
		margin: 0.5rem 0;
	}

	.analysis-content :global(li) {
		margin: 0.25rem 0;
	}

	.analysis-content :global(p) {
		margin: 0.5rem 0;
	}

	.analysis-content :global(code) {
		background: #1e293b;
		padding: 0.1rem 0.3rem;
		border-radius: 3px;
		font-size: 0.8125rem;
	}

	.negative {
		color: #fca5a5;
	}

	/* Greek signal colors */
	:global(.signal-fantastic) {
		color: #60a5fa;
		font-weight: 600;
	}

	:global(.signal-ok) {
		color: #4ade80;
	}

	:global(.signal-warning) {
		color: #fbbf24;
		font-weight: 600;
	}

	:global(.signal-danger) {
		color: #f87171;
		font-weight: 600;
	}
</style>
