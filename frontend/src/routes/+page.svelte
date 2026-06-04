<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { getAuthStatus, importPositions, fetchPositionsFromIB, analyzeSymbol, logout, refreshGex, getBlacklist, listApiKeys, createApiKey, deleteApiKey, type BlacklistEntry, type ApiKeyListItem, type ApiKeyCreated } from '$lib/api';
	import { PortfolioWebSocket, type PortfolioUpdate, type PortfolioRollup, type UnderlyingSummary, type GEXProfile } from '$lib/ws';
	import { evaluateAll, evaluateNetDelta, evaluateNetTheta, evaluateNetVega, evaluateNetGamma, evaluateBetaWeightedDelta, signalClass } from '$lib/greek-signals';
	import { tooltip } from '$lib/tooltip';
	import { marked } from 'marked';
	import GexChart from '$lib/GexChart.svelte';

	let authenticated = $state(false);
	let hasCredentials = $state(false);
	let loading = $state(true);
	let underlyings = $state<Record<string, UnderlyingSummary>>({});
	let connected = $state(false);
	let importMessage = $state('');
	let lastUpdated = $state('');
	let lastReportGenerated = $state<string | null>(null);
	let blacklist = $state<BlacklistEntry[]>([]);
	let blacklistLoading = $state(false);
	let blacklistError = $state('');
	let washsaleQuery = $state('');
	let marketOpen = $state(false);
	let portfolio = $state<PortfolioRollup>({ net_delta: 0, net_gamma: 0, net_theta: 0, net_vega: 0, beta_weighted_delta: 0, spy_price: 0 });
	let gexProfiles = $state<Record<string, GEXProfile>>({});
	let gexRefreshing = $state(false);
	let gexError = $state('');
	let analyses = $state<Record<string, string>>({});
	let analyzing = $state<Record<string, boolean>>({});
	let analysisVisible = $state<Record<string, boolean>>({});

	function loadFilterPref(key: string): boolean {
		try {
			const raw = localStorage.getItem(key);
			return raw === null ? true : raw === 'true';
		} catch {
			return true;
		}
	}

	let showStocks = $state(loadFilterPref('po_show_stocks'));
	let showOptions = $state(loadFilterPref('po_show_options'));

	$effect(() => {
		try { localStorage.setItem('po_show_stocks', String(showStocks)); } catch {}
	});
	$effect(() => {
		try { localStorage.setItem('po_show_options', String(showOptions)); } catch {}
	});

	// Auto-dismiss the import message after 30s. The ring around the
	// close button animates at the same duration so visual and JS timers
	// stay in sync.
	let importDismissTimer: ReturnType<typeof setTimeout> | undefined;
	$effect(() => {
		if (importDismissTimer) {
			clearTimeout(importDismissTimer);
			importDismissTimer = undefined;
		}
		if (importMessage) {
			importDismissTimer = setTimeout(() => {
				importMessage = '';
			}, 30000);
		}
	});

	function shouldShowPosition(contractType: string): boolean {
		if (contractType === 'stock') return showStocks;
		return showOptions; // call, put
	}

	const visibleUnderlyings = $derived.by<Record<string, UnderlyingSummary>>(() => {
		const result: Record<string, UnderlyingSummary> = {};
		const spyPrice = portfolio.spy_price;
		for (const [ticker, summary] of Object.entries(underlyings)) {
			const positions = summary.positions.filter(
				p => shouldShowPosition(p.contract_type)
			);
			if (positions.length === 0) continue;

			let nd = 0, ng = 0, nt = 0, nv = 0;
			let underlyingPrice = 0;
			for (const pos of positions) {
				const m = pos.multiplier;
				const q = pos.quantity;
				nd += pos.greeks.delta * q * m;
				ng += pos.greeks.gamma * q * m;
				nt += pos.greeks.theta * q * m;
				nv += pos.greeks.vega * q * m;
				if (!underlyingPrice && pos.underlying_price) {
					underlyingPrice = pos.underlying_price;
				}
			}
			const bw = spyPrice > 0
				? nd * summary.beta * (underlyingPrice / spyPrice)
				: 0;

			const visibleSymbols = new Set(positions.map(p => p.symbol));
			const advice = summary.advice.filter(
				a => visibleSymbols.has(a.position_symbol) || a.position_symbol === ticker
			);

			result[ticker] = {
				...summary,
				positions,
				advice,
				net_delta: nd,
				net_gamma: ng,
				net_theta: nt,
				net_vega: nv,
				beta_weighted_delta: bw,
			};
		}
		return result;
	});

	const visiblePortfolio = $derived.by<PortfolioRollup>(() => {
		let nd = 0, ng = 0, nt = 0, nv = 0, bw = 0;
		for (const summary of Object.values(visibleUnderlyings)) {
			nd += summary.net_delta;
			ng += summary.net_gamma;
			nt += summary.net_theta;
			nv += summary.net_vega;
			bw += summary.beta_weighted_delta;
		}
		return {
			net_delta: nd,
			net_gamma: ng,
			net_theta: nt,
			net_vega: nv,
			beta_weighted_delta: bw,
			spy_price: portfolio.spy_price,
		};
	});
	let apiKeys = $state<ApiKeyListItem[]>([]);
	let apiKeysLoading = $state(false);
	let apiKeysError = $state('');
	let newApiKeyName = $state('');
	let creatingApiKey = $state(false);
	let recentlyCreatedKey = $state<ApiKeyCreated | null>(null);
	let copyKeySuccess = $state(false);

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

	type Tab = 'greeks' | 'washsale' | 'apikeys';
	const TAB_KEY = 'po_active_tab';

	function loadActiveTab(): Tab {
		try {
			const raw = localStorage.getItem(TAB_KEY);
			if (raw === 'greeks' || raw === 'washsale' || raw === 'apikeys') return raw;
		} catch {
			// ignore
		}
		return 'greeks';
	}

	let activeTab = $state<Tab>(loadActiveTab());

	function setActiveTab(tab: Tab) {
		activeTab = tab;
		try {
			localStorage.setItem(TAB_KEY, tab);
		} catch {
			// ignore
		}
		if (tab === 'washsale') {
			loadBlacklist();
		} else if (tab === 'apikeys') {
			loadApiKeys();
		}
	}

	async function loadApiKeys() {
		if (!authenticated) return;
		apiKeysLoading = true;
		apiKeysError = '';
		try {
			const result = await listApiKeys();
			apiKeys = result.keys;
		} catch (e) {
			apiKeysError = `Failed to load API keys: ${e}`;
		} finally {
			apiKeysLoading = false;
		}
	}

	async function handleCreateApiKey(event: SubmitEvent) {
		event.preventDefault();
		const name = newApiKeyName.trim();
		if (!name || creatingApiKey) return;
		creatingApiKey = true;
		apiKeysError = '';
		try {
			const created = await createApiKey(name);
			recentlyCreatedKey = created;
			newApiKeyName = '';
			await loadApiKeys();
		} catch (e) {
			apiKeysError = `Failed to create API key: ${e}`;
		} finally {
			creatingApiKey = false;
		}
	}

	async function handleRevokeApiKey(key: ApiKeyListItem) {
		const ok = confirm(
			`Revoke "${key.name}" (${key.key_prefix}…)?\n\n` +
			'Any process using this key will lose access immediately.'
		);
		if (!ok) return;
		try {
			await deleteApiKey(key.id);
			if (recentlyCreatedKey?.id === key.id) {
				recentlyCreatedKey = null;
			}
			await loadApiKeys();
		} catch (e) {
			apiKeysError = `Failed to revoke key: ${e}`;
		}
	}

	async function copyKeyToClipboard() {
		if (!recentlyCreatedKey) return;
		try {
			await navigator.clipboard.writeText(recentlyCreatedKey.key);
			copyKeySuccess = true;
			setTimeout(() => { copyKeySuccess = false; }, 2000);
		} catch (e) {
			apiKeysError = `Copy failed: ${e}`;
		}
	}

	function dismissCreatedKey() {
		recentlyCreatedKey = null;
		copyKeySuccess = false;
	}

	function formatApiKeyTimestamp(iso: string | null): string {
		if (!iso) return '—';
		try {
			return new Date(iso).toLocaleString();
		} catch {
			return iso;
		}
	}

	async function loadBlacklist() {
		if (!authenticated) return;
		blacklistLoading = true;
		blacklistError = '';
		try {
			const result = await getBlacklist();
			blacklist = result.entries;
		} catch (e) {
			blacklistError = `Failed to load blacklist: ${e}`;
		} finally {
			blacklistLoading = false;
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

	function handleUnauthorized() {
		if (!authenticated) return;
		authenticated = false;
		ws?.disconnect();
		underlyings = {};
		importMessage = '';
	}

	onMount(async () => {
		window.addEventListener('po:unauthorized', handleUnauthorized);
		try {
			const status = await getAuthStatus();
			authenticated = status.authenticated;
			hasCredentials = status.has_credentials;

			if (authenticated) {
				startWebSocket();
				handleFetchFromIB(false);
				if (activeTab === 'washsale') {
					loadBlacklist();
				} else if (activeTab === 'apikeys') {
					loadApiKeys();
				}
			}
		} catch (e) {
			console.error('Failed to check auth status:', e);
		} finally {
			loading = false;
		}
	});

	onDestroy(() => {
		window.removeEventListener('po:unauthorized', handleUnauthorized);
		ws?.disconnect();
	});

	function startWebSocket() {
		ws = new PortfolioWebSocket();
		ws.onMessage((data: PortfolioUpdate) => {
			if (data.type === 'update') {
				underlyings = data.underlyings;
				lastUpdated = data.last_updated;
				lastReportGenerated = data.last_report_generated;
				marketOpen = data.market_open;
				portfolio = data.portfolio;
				if (data.gex) {
					gexProfiles = data.gex;
					gexRefreshing = false;
				}
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

	let isDragging = $state(false);
	let importing = $state(false);
	let dragCounter = 0;
	let uploadDialog: HTMLDialogElement | undefined = $state();
	let uploadDialogFile = $state<File | null>(null);

	function openUploadDialog() {
		uploadDialogFile = null;
		uploadDialog?.showModal();
	}

	function closeUploadDialog() {
		uploadDialog?.close();
		uploadDialogFile = null;
	}

	function handleUploadFileChange(event: Event) {
		const input = event.target as HTMLInputElement;
		uploadDialogFile = input.files?.[0] ?? null;
	}

	async function handleUploadSubmit(event: SubmitEvent) {
		event.preventDefault();
		if (!uploadDialogFile || importing) return;
		const file = uploadDialogFile;
		closeUploadDialog();
		await importFile(file);
	}

	async function importFile(file: File) {
		if (importing) return;
		importing = true;
		try {
			const result = await importPositions(file);
			importMessage = `Imported ${result.imported} positions from ${file.name}`;
			ws?.requestRefresh();
			loadBlacklist();
		} catch (e) {
			importMessage = `Import failed: ${e}`;
		} finally {
			importing = false;
		}
	}

	function dragHasFiles(event: DragEvent): boolean {
		return !!event.dataTransfer && Array.from(event.dataTransfer.types).includes('Files');
	}

	function handleDragEnter(event: DragEvent) {
		if (!dragHasFiles(event)) return;
		event.preventDefault();
		if (!authenticated) return;
		dragCounter += 1;
		isDragging = true;
	}

	function handleDragOver(event: DragEvent) {
		if (!dragHasFiles(event)) return;
		event.preventDefault();
		if (!authenticated) return;
		if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
	}

	function handleDragLeave(event: DragEvent) {
		if (!dragHasFiles(event)) return;
		event.preventDefault();
		if (!authenticated) return;
		dragCounter = Math.max(0, dragCounter - 1);
		if (dragCounter === 0) isDragging = false;
	}

	async function handleDrop(event: DragEvent) {
		if (!dragHasFiles(event)) return;
		event.preventDefault();
		if (!authenticated) return;
		dragCounter = 0;
		isDragging = false;

		const file = event.dataTransfer?.files?.[0];
		if (!file) return;

		const looksLikeXml =
			file.type === 'text/xml' ||
			file.type === 'application/xml' ||
			file.name.toLowerCase().endsWith('.xml');
		if (!looksLikeXml) {
			importMessage = `Import failed: "${file.name}" doesn't look like an XML file`;
			return;
		}

		await importFile(file);
	}

	let fetching = $state(false);

	function formatReportAge(iso: string | null): string {
		if (!iso) return 'unknown';
		const reportTime = new Date(iso).getTime();
		if (Number.isNaN(reportTime)) return iso;
		const diffSec = Math.max(0, Math.floor((Date.now() - reportTime) / 1000));
		if (diffSec < 60) return `${diffSec}s ago`;
		if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
		if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
		return `${Math.floor(diffSec / 86400)}d ago`;
	}

	async function handleFetchFromIB(force: boolean = true) {
		fetching = true;
		importMessage = '';
		try {
			const result = await fetchPositionsFromIB(force);
			if (result.stale && result.error) {
				const age = formatReportAge(result.report_generated_at);
				importMessage = `Showing cached positions (report ${age}). Fresh fetch failed: ${result.error}`;
			} else {
				const label = result.cached ? 'Loaded' : 'Fetched';
				importMessage = `${label} ${result.imported} positions from IB`;
			}
			ws?.requestRefresh();
			loadBlacklist();
		} catch (e) {
			importMessage = `Fetch failed: ${e}`;
		} finally {
			fetching = false;
		}
	}

	async function handleGexRefresh() {
		gexRefreshing = true;
		gexError = '';
		try {
			await refreshGex();
			// REST returns immediately; data arrives via WebSocket.
			// gexRefreshing stays true until WebSocket delivers GEX data
			// (or timeout after 5 minutes as a safety net).
			setTimeout(() => {
				if (gexRefreshing) {
					gexRefreshing = false;
					if (Object.keys(gexProfiles).length === 0) {
						gexError = 'GEX refresh timed out. Check server logs.';
					}
				}
			}, 300000);
		} catch (e) {
			gexError = `GEX refresh failed: ${e}`;
			gexRefreshing = false;
		}
	}

	function getLiveSpot(ticker: string): number {
		const summary = underlyings[ticker];
		if (summary?.positions?.[0]?.underlying_price) {
			return summary.positions[0].underlying_price;
		}
		if (ticker === 'SPY' && portfolio.spy_price) {
			return portfolio.spy_price;
		}
		return 0;
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

	function formatReportTimestamp(iso: string | null): string {
		if (!iso) return '';
		const d = new Date(iso);
		return d.toLocaleString('en-US', {
			year: 'numeric',
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit',
			timeZoneName: 'short',
		});
	}
</script>

<svelte:window
	ondragenter={handleDragEnter}
	ondragover={handleDragOver}
	ondragleave={handleDragLeave}
	ondrop={handleDrop}
/>

{#if isDragging}
	<div class="drop-overlay">
		<div class="drop-overlay-inner">
			<div class="drop-icon">⬇</div>
			<div class="drop-title">Drop Flex Query XML to import</div>
			<div class="drop-subtitle">Releases the file and replaces your position set</div>
		</div>
	</div>
{/if}

<dialog bind:this={uploadDialog} class="upload-dialog">
	<form method="dialog" class="upload-dialog-form" onsubmit={handleUploadSubmit}>
		<h3>Upload Flex Report</h3>
		<p class="muted">
			Select an IB Flex Query XML file. This replaces your current
			position set with the file's contents.
		</p>
		<input
			type="file"
			accept=".xml,application/xml,text/xml"
			onchange={handleUploadFileChange}
			required
		/>
		<div class="upload-dialog-actions">
			<button type="button" class="upload-cancel" onclick={closeUploadDialog}>
				Cancel
			</button>
			<button
				type="submit"
				class="upload-submit"
				disabled={!uploadDialogFile || importing}
			>
				{importing ? 'Uploading…' : 'Upload'}
			</button>
		</div>
	</form>
</dialog>

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
			<button class="btn btn-secondary" onclick={() => handleFetchFromIB()} disabled={fetching}>
				{fetching ? 'Fetching...' : 'Fetch from IB'}
			</button>
			<button class="btn btn-ghost" onclick={handleLogout}>Logout</button>
		</div>
	</header>

	{#if importMessage}
		{#key importMessage}
			<div class="import-message">
				<span>{importMessage}</span>
				<button
					class="import-message-close"
					onclick={() => importMessage = ''}
					aria-label="Dismiss notification"
				>
					<svg class="countdown-ring" viewBox="0 0 28 28" aria-hidden="true">
						<circle class="countdown-ring-progress" cx="14" cy="14" r="13" />
					</svg>
					<span class="import-message-close-x" aria-hidden="true">&times;</span>
				</button>
			</div>
		{/key}
	{/if}

	<div class="tabs" role="tablist" aria-label="App sections">
		<button
			class="tab"
			class:tab-active={activeTab === 'greeks'}
			role="tab"
			aria-selected={activeTab === 'greeks'}
			onclick={() => setActiveTab('greeks')}
		>
			Greeks
		</button>
		<button
			class="tab"
			class:tab-active={activeTab === 'washsale'}
			role="tab"
			aria-selected={activeTab === 'washsale'}
			onclick={() => setActiveTab('washsale')}
		>
			Wash-Sale Watcher
		</button>
		<button
			class="tab"
			class:tab-active={activeTab === 'apikeys'}
			role="tab"
			aria-selected={activeTab === 'apikeys'}
			onclick={() => setActiveTab('apikeys')}
		>
			API Keys
		</button>
	</div>

	{#if activeTab === 'greeks'}
	<main>
		{#if Object.keys(underlyings).length === 0}
			<div class="empty">
				<p>No positions loaded. Import a Flex Query XML to get started.</p>
			</div>
		{:else}
			<div class="market-section">
				<div class="market-header">
					<span class="market-label">Market GEX</span>
					<button
						class="btn btn-secondary btn-sm"
						onclick={handleGexRefresh}
						disabled={gexRefreshing}
					>
						{gexRefreshing ? 'Loading GEX...' : 'Refresh GEX'}
					</button>
				</div>
				{#if gexProfiles['SPY']}
					<div class="gex-grid">
						<GexChart profile={gexProfiles['SPY']} liveSpot={getLiveSpot('SPY')} />
					</div>
				{:else}
					<div class="gex-empty">
						{#if gexRefreshing}
							<span class="gex-loading">Fetching options chain data...</span>
						{:else if gexError}
							<span class="gex-error">{gexError}</span>
						{:else}
							<span class="gex-hint">No GEX data yet. Click "Refresh GEX" to load.</span>
						{/if}
					</div>
				{/if}
			</div>

			{@const pt = evaluateNetTheta(visiblePortfolio.net_theta)}
			{@const pv = evaluateNetVega(visiblePortfolio.net_vega)}
			{@const pg_ = evaluateNetGamma(visiblePortfolio.net_gamma)}
			{@const pbw = evaluateBetaWeightedDelta(visiblePortfolio.beta_weighted_delta)}
			<div class="portfolio-bar">
				<div class="portfolio-left">
					<span class="portfolio-label">Portfolio</span>
					<div class="filter-bar">
						<label class="filter-toggle">
							<input type="checkbox" bind:checked={showStocks} />
							<span class="filter-pill">
								<span class="filter-check" aria-hidden="true">&#10003;</span>
								Show Equities
							</span>
						</label>
						<label class="filter-toggle">
							<input type="checkbox" bind:checked={showOptions} />
							<span class="filter-pill">
								<span class="filter-check" aria-hidden="true">&#10003;</span>
								Show Options
							</span>
						</label>
					</div>
				</div>
				<div class="net-greeks">
					<span class="greek-badge {signalClass(pbw.level)}" use:tooltip={pbw.reason}>
						SPY &Delta; {formatGreek(visiblePortfolio.beta_weighted_delta, 2)}
					</span>
					<span class="greek-badge {signalClass(pt.level)}" use:tooltip={pt.reason}>
						&Theta; {formatGreek(visiblePortfolio.net_theta, 2)}
					</span>
					<span class="greek-badge {signalClass(pv.level)}" use:tooltip={pv.reason}>
						V {formatGreek(visiblePortfolio.net_vega, 2)}
					</span>
					<span class="greek-badge {signalClass(pg_.level)}" use:tooltip={pg_.reason}>
						&Gamma; {formatGreek(visiblePortfolio.net_gamma, 2)}
					</span>
				</div>
			</div>

		{#if Object.keys(visibleUnderlyings).length === 0}
			<div class="empty">
				<p>Nothing to show. Toggle one of the filters above to see positions.</p>
			</div>
		{:else}
			{#each Object.entries(visibleUnderlyings).sort(([a], [b]) => a.localeCompare(b)) as [ticker, summary]}
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
					{#if gexProfiles[ticker]}
						<div class="underlying-gex">
							<GexChart profile={gexProfiles[ticker]} compact={true} liveSpot={getLiveSpot(ticker)} />
						</div>
					{/if}

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
									<th use:tooltip={'Type — Call, Put, or Stock.'}>Type</th>
									<th use:tooltip={'Strike — Contract strike price.'}>Strike</th>
									<th use:tooltip={'Expiration — Contract expiration date.'}>Exp</th>
									<th use:tooltip={'Quantity — Number of contracts (negative = short).'}>Qty</th>
									<th use:tooltip={'P&L % — Direction-aware profit/loss as a fraction of entry premium, using a Black-Scholes theoretical mid from live IV. For shorts, positive = premium decaying in your favor; 80%+ is candidate-to-close territory.'}>P&amp;L %</th>
									<th use:tooltip={'Implied Volatility — Annualized vol the market is currently pricing into this contract.'}>IV</th>
									<th use:tooltip={'VRP — Volatility Risk Premium ratio: trailing 21-day realized vol divided by the IV implied by your entry premium. Below 1.0 favors short positions; above 1.0 favors long positions.'}>VRP</th>
									<th use:tooltip={'Delta — Change in option price per $1 move in the underlying (per contract).'}>&Delta;</th>
									<th use:tooltip={'Theta — Daily change in option price from time decay (per contract, per calendar day).'}>&Theta;</th>
									<th use:tooltip={'Vega — Change in option price per 1% change in implied volatility (per contract).'}>Vega</th>
									<th use:tooltip={'Gamma — Change in delta per $1 move in the underlying (per contract).'}>&Gamma;</th>
									<th use:tooltip={'Vanna — Change in delta per 1% change in implied volatility (per contract).'}>Vanna</th>
									<th use:tooltip={'Charm — Change in delta per calendar day (per contract).'}>Charm</th>
									<th use:tooltip={'Vomma — Change in vega per 1% change in implied volatility (per contract).'}>Vomma</th>
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
										<td class={signalClass(signals.pnl.level)} use:tooltip={signals.pnl.reason}>
											{pos.pnl_pct == null ? '—' : (pos.pnl_pct * 100).toFixed(0) + '%'}
										</td>
										<td>{isStock ? '—' : (pos.greeks.implied_volatility * 100).toFixed(1) + '%'}</td>
										<td class={signalClass(signals.vrp.level)} use:tooltip={signals.vrp.reason}>
											{isStock || pos.vrp == null ? '—' : pos.vrp.toFixed(2)}
										</td>
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
		{/if}

		{#if lastReportGenerated}
			<div class="last-import">
				Report generated: {formatReportTimestamp(lastReportGenerated)}
				<span class="last-import-sep">·</span>
				<button
					type="button"
					class="text-link"
					onclick={openUploadDialog}
				>Upload new report</button>
			</div>
		{/if}
	</main>
	{:else if activeTab === 'washsale'}
	<main class="washsale">
		<div class="ws-header">
			<h2>Wash-Sale Watcher</h2>
			<p class="muted">
				Symbols where you've realized a loss in the last 30 days. Buying any of
				these triggers a wash sale under IRS rules.
			</p>
		</div>

		<div class="ws-search">
			<input
				type="text"
				placeholder="Check a symbol (e.g. AAPL)"
				bind:value={washsaleQuery}
				autocomplete="off"
				spellcheck="false"
			/>
			{#if washsaleQuery.trim()}
				{@const q = washsaleQuery.trim().toUpperCase()}
				{@const hit = blacklist.find(e => e.symbol === q)}
				{#if hit}
					<div class="ws-verdict ws-verdict-blocked">
						<strong>{q} — BLOCKED</strong> until {hit.expires}
						<span class="muted">({hit.days_remaining}d remaining)</span>
					</div>
				{:else}
					<div class="ws-verdict ws-verdict-clear">
						<strong>{q} — CLEAR</strong>
						<span class="muted">No wash-sale risk in the current 30-day window</span>
					</div>
				{/if}
			{/if}
		</div>

		{#if blacklistError}
			<div class="ws-error">{blacklistError}</div>
		{:else if blacklistLoading && blacklist.length === 0}
			<div class="ws-empty">Loading…</div>
		{:else if blacklist.length === 0}
			<div class="ws-empty">
				No symbols blacklisted. Fetch a Flex Query that contains realized
				losses to populate this list.
			</div>
		{:else}
			{@const filtered = washsaleQuery.trim()
				? blacklist.filter(e => e.symbol.includes(washsaleQuery.trim().toUpperCase()))
				: blacklist}
			<table class="ws-table">
				<thead>
					<tr>
						<th>Symbol</th>
						<th>Loss Date</th>
						<th>Expires</th>
						<th class="num">Days Remaining</th>
					</tr>
				</thead>
				<tbody>
					{#each filtered as entry (entry.symbol)}
						<tr>
							<td class="ws-symbol">{entry.symbol}</td>
							<td>{entry.loss_date}</td>
							<td>{entry.expires}</td>
							<td class="num" class:ws-imminent={entry.days_remaining < 7}>
								{entry.days_remaining}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
			{#if filtered.length === 0}
				<div class="ws-empty">No symbols match "{washsaleQuery}".</div>
			{/if}
		{/if}

		{#if lastReportGenerated}
			<div class="last-import">
				Report generated: {formatReportTimestamp(lastReportGenerated)}
				<span class="last-import-sep">·</span>
				<button
					type="button"
					class="text-link"
					onclick={openUploadDialog}
				>Upload new report</button>
			</div>
		{/if}
	</main>
	{:else if activeTab === 'apikeys'}
	<main class="apikeys">
		<div class="apikeys-header">
			<h2>API Keys</h2>
			<p class="muted">
				API keys grant programmatic, read-and-write access to your positions
				and wash-sale data via the REST API at <code>/api/v1/</code>. Send the
				key as a Bearer token: <code>Authorization: Bearer po_…</code>.
				Browse <a href="/docs" target="_blank" rel="noopener">/docs</a> for the
				full API reference.
			</p>
		</div>

		<form class="apikey-create" onsubmit={handleCreateApiKey}>
			<input
				type="text"
				placeholder="Key name (e.g. reporting-server)"
				bind:value={newApiKeyName}
				maxlength="64"
				autocomplete="off"
				spellcheck="false"
				disabled={creatingApiKey}
			/>
			<button
				type="submit"
				class="primary"
				disabled={creatingApiKey || !newApiKeyName.trim()}
			>
				{creatingApiKey ? 'Generating…' : 'Generate new key'}
			</button>
		</form>

		{#if recentlyCreatedKey}
			<div class="apikey-revealed">
				<div class="apikey-revealed-header">
					<strong>{recentlyCreatedKey.name}</strong> created. Copy the key
					below NOW — it cannot be retrieved again.
				</div>
				<div class="apikey-revealed-body">
					<code class="apikey-cleartext">{recentlyCreatedKey.key}</code>
					<button
						type="button"
						class="apikey-copy"
						onclick={copyKeyToClipboard}
					>
						{copyKeySuccess ? 'Copied!' : 'Copy'}
					</button>
					<button
						type="button"
						class="apikey-dismiss"
						onclick={dismissCreatedKey}
						aria-label="Dismiss"
					>×</button>
				</div>
			</div>
		{/if}

		{#if apiKeysError}
			<div class="ws-error">{apiKeysError}</div>
		{/if}

		{#if apiKeysLoading && apiKeys.length === 0}
			<div class="ws-empty">Loading…</div>
		{:else if apiKeys.length === 0}
			<div class="ws-empty">
				No API keys yet. Generate one above to access the REST API.
			</div>
		{:else}
			<table class="apikey-table">
				<thead>
					<tr>
						<th>Name</th>
						<th>Prefix</th>
						<th>Created</th>
						<th>Last used</th>
						<th></th>
					</tr>
				</thead>
				<tbody>
					{#each apiKeys as key (key.id)}
						<tr>
							<td class="apikey-name">{key.name}</td>
							<td><code>{key.key_prefix}…</code></td>
							<td>{formatApiKeyTimestamp(key.created_at)}</td>
							<td>{formatApiKeyTimestamp(key.last_used_at)}</td>
							<td>
								<button
									type="button"
									class="apikey-revoke"
									onclick={() => handleRevokeApiKey(key)}
								>
									Revoke
								</button>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</main>
	{/if}
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

	.last-import {
		text-align: center;
		color: #64748b;
		font-size: 0.8125rem;
		padding: 1.25rem 0 0.5rem;
		font-variant-numeric: tabular-nums;
	}

	.last-import-sep {
		margin: 0 0.4rem;
		color: #475569;
	}

	.text-link {
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		color: #60a5fa;
		text-decoration: underline;
		cursor: pointer;
	}

	.text-link:hover {
		color: #93c5fd;
	}

	.upload-dialog {
		border: 1px solid #334155;
		border-radius: 8px;
		background: #1e293b;
		color: #e2e8f0;
		padding: 0;
		max-width: 480px;
		width: calc(100% - 2rem);
	}

	.upload-dialog::backdrop {
		background: rgba(0, 0, 0, 0.6);
	}

	.upload-dialog-form {
		padding: 1.5rem;
		display: flex;
		flex-direction: column;
		gap: 1rem;
	}

	.upload-dialog-form h3 {
		margin: 0;
		color: #f1f5f9;
		font-size: 1.1rem;
	}

	.upload-dialog-form .muted {
		margin: 0;
		color: #94a3b8;
		font-size: 0.9rem;
		line-height: 1.5;
	}

	.upload-dialog-form input[type='file'] {
		padding: 0.5rem;
		background: #0f172a;
		border: 1px solid #334155;
		border-radius: 4px;
		color: #cbd5e1;
		font-size: 0.9rem;
	}

	.upload-dialog-form input[type='file']::file-selector-button {
		margin-right: 0.75rem;
		padding: 0.35rem 0.75rem;
		background: #334155;
		border: none;
		border-radius: 3px;
		color: #e2e8f0;
		font-size: 0.85rem;
		cursor: pointer;
	}

	.upload-dialog-form input[type='file']::file-selector-button:hover {
		background: #475569;
	}

	.upload-dialog-actions {
		display: flex;
		gap: 0.5rem;
		justify-content: flex-end;
	}

	.upload-cancel,
	.upload-submit {
		padding: 0.5rem 1rem;
		border-radius: 4px;
		font-size: 0.9rem;
		cursor: pointer;
		font-weight: 600;
	}

	.upload-cancel {
		background: transparent;
		border: 1px solid #475569;
		color: #cbd5e1;
	}

	.upload-cancel:hover {
		background: #334155;
	}

	.upload-submit {
		background: #2563eb;
		border: none;
		color: white;
	}

	.upload-submit:hover:not(:disabled) {
		background: #1d4ed8;
	}

	.upload-submit:disabled {
		background: #334155;
		color: #94a3b8;
		cursor: not-allowed;
	}

	.drop-overlay {
		position: fixed;
		inset: 0;
		z-index: 1000;
		background: rgba(15, 23, 42, 0.85);
		backdrop-filter: blur(2px);
		display: flex;
		align-items: center;
		justify-content: center;
		pointer-events: none;
	}

	.drop-overlay-inner {
		border: 2px dashed #60a5fa;
		border-radius: 1rem;
		padding: 3rem 5rem;
		background: rgba(30, 58, 95, 0.6);
		text-align: center;
		color: #dbeafe;
	}

	.drop-icon {
		font-size: 3rem;
		line-height: 1;
		margin-bottom: 0.75rem;
		color: #60a5fa;
	}

	.drop-title {
		font-size: 1.25rem;
		font-weight: 600;
		margin-bottom: 0.25rem;
	}

	.drop-subtitle {
		font-size: 0.875rem;
		color: #93c5fd;
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
		position: relative;
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

	.import-message-close-x {
		position: relative;
		z-index: 1;
	}

	.countdown-ring {
		position: absolute;
		inset: -2px;
		width: calc(100% + 4px);
		height: calc(100% + 4px);
		pointer-events: none;
		/* CSS transforms apply right-to-left: scaleX(-1) first reverses
		   the drawing direction, then rotate(90deg) puts the start point
		   at 12 o'clock. Net result: line grows counter-clockwise from
		   the top. */
		transform: rotate(90deg) scaleX(-1);
		overflow: visible;
	}

	.countdown-ring-progress {
		fill: none;
		stroke: #93c5fd;
		stroke-width: 1.5;
		stroke-linecap: round;
		/* 2π × 13 ≈ 81.68; full dash hidden, then animated to fully drawn. */
		stroke-dasharray: 81.68;
		stroke-dashoffset: 81.68;
		animation: countdown-ring-fill 30s linear forwards;
	}

	@keyframes countdown-ring-fill {
		to { stroke-dashoffset: 0; }
	}

	.tabs {
		display: flex;
		gap: 0.25rem;
		padding: 0 2rem;
		border-bottom: 1px solid #1e293b;
		background: #0f172a;
	}

	.tab {
		background: transparent;
		border: none;
		border-bottom: 2px solid transparent;
		color: #94a3b8;
		padding: 0.75rem 1rem;
		font-size: 0.875rem;
		font-weight: 600;
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s, background 0.15s;
		margin-bottom: -1px;
	}

	.tab:hover {
		color: #e2e8f0;
		background: #111c2e;
	}

	.tab-active {
		color: #93c5fd;
		border-bottom-color: #3b82f6;
	}

	.tab-active:hover {
		color: #93c5fd;
		background: transparent;
	}

	.washsale {
		max-width: 60rem;
		margin: 0 auto;
		width: 100%;
		gap: 1.25rem;
	}

	.ws-header h2 {
		margin: 0 0 0.25rem;
		color: #e2e8f0;
	}

	.ws-header .muted {
		font-size: 0.875rem;
		color: #94a3b8;
		margin: 0;
	}

	.ws-search {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.ws-search input {
		background: #0f172a;
		border: 1px solid #334155;
		border-radius: 0.375rem;
		padding: 0.6rem 0.85rem;
		color: #e2e8f0;
		font-size: 0.95rem;
		font-family: inherit;
		font-variant-numeric: tabular-nums;
		text-transform: uppercase;
	}

	.ws-search input:focus {
		outline: none;
		border-color: #3b82f6;
	}

	.ws-verdict {
		padding: 0.75rem 1rem;
		border-radius: 0.375rem;
		font-size: 0.95rem;
		display: flex;
		gap: 0.75rem;
		align-items: baseline;
		flex-wrap: wrap;
	}

	.ws-verdict .muted {
		color: rgba(255, 255, 255, 0.7);
		font-size: 0.85rem;
	}

	.ws-verdict-blocked {
		background: #3f1d1d;
		color: #fca5a5;
		border: 1px solid #7f1d1d;
	}

	.ws-verdict-clear {
		background: #14322a;
		color: #86efac;
		border: 1px solid #166534;
	}

	.ws-empty,
	.ws-error {
		padding: 1.25rem;
		text-align: center;
		color: #94a3b8;
		background: #0f172a;
		border-radius: 0.375rem;
	}

	.ws-error {
		color: #fca5a5;
	}

	.ws-table {
		width: 100%;
		border-collapse: collapse;
		background: #0f172a;
		border-radius: 0.375rem;
		overflow: hidden;
	}

	.ws-table th,
	.ws-table td {
		padding: 0.6rem 0.85rem;
		text-align: left;
		font-size: 0.9rem;
		border-bottom: 1px solid #1e293b;
	}

	.ws-table thead th {
		background: #111c2e;
		color: #94a3b8;
		font-weight: 600;
		font-size: 0.8rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.ws-table tbody tr:last-child td {
		border-bottom: none;
	}

	.ws-table .num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.ws-symbol {
		font-weight: 600;
		color: #e2e8f0;
	}

	.ws-imminent {
		color: #fca5a5;
		font-weight: 600;
	}

	.apikeys {
		max-width: 960px;
		margin: 0 auto;
		width: 100%;
	}

	.apikeys-header h2 {
		margin: 0 0 0.5rem;
		color: #e2e8f0;
	}

	.apikeys-header .muted {
		color: #94a3b8;
		font-size: 0.9rem;
		line-height: 1.5;
	}

	.apikeys-header code {
		background: #1e293b;
		padding: 0.1rem 0.4rem;
		border-radius: 3px;
		font-size: 0.85em;
	}

	.apikeys-header a {
		color: #93c5fd;
	}

	.apikey-create {
		display: flex;
		gap: 0.75rem;
		align-items: center;
		margin-top: 1rem;
		padding: 1rem;
		background: #1e293b;
		border-radius: 6px;
	}

	.apikey-create input {
		flex: 1;
		padding: 0.6rem 0.8rem;
		background: #0f172a;
		border: 1px solid #334155;
		border-radius: 4px;
		color: #e2e8f0;
		font-size: 0.95rem;
	}

	.apikey-create input:focus {
		outline: none;
		border-color: #60a5fa;
	}

	.apikey-create button.primary {
		padding: 0.6rem 1.2rem;
		background: #2563eb;
		border: none;
		border-radius: 4px;
		color: white;
		font-weight: 600;
		cursor: pointer;
		font-size: 0.9rem;
	}

	.apikey-create button.primary:disabled {
		background: #334155;
		color: #94a3b8;
		cursor: not-allowed;
	}

	.apikey-create button.primary:not(:disabled):hover {
		background: #1d4ed8;
	}

	.apikey-revealed {
		margin-top: 1rem;
		padding: 1rem;
		background: #1c2f1c;
		border: 1px solid #4ade80;
		border-radius: 6px;
		color: #bbf7d0;
	}

	.apikey-revealed-header {
		font-size: 0.9rem;
		margin-bottom: 0.75rem;
	}

	.apikey-revealed-body {
		display: flex;
		gap: 0.5rem;
		align-items: center;
	}

	.apikey-cleartext {
		flex: 1;
		font-family: ui-monospace, SFMono-Regular, monospace;
		font-size: 0.9rem;
		padding: 0.5rem 0.75rem;
		background: #0f172a;
		border-radius: 4px;
		color: #f1f5f9;
		word-break: break-all;
		user-select: all;
	}

	.apikey-copy {
		padding: 0.5rem 1rem;
		background: #16a34a;
		border: none;
		border-radius: 4px;
		color: white;
		font-weight: 600;
		font-size: 0.85rem;
		cursor: pointer;
		min-width: 80px;
	}

	.apikey-copy:hover {
		background: #15803d;
	}

	.apikey-dismiss {
		padding: 0 0.6rem;
		background: transparent;
		border: 1px solid #4ade80;
		border-radius: 4px;
		color: #bbf7d0;
		font-size: 1.2rem;
		line-height: 1;
		cursor: pointer;
	}

	.apikey-dismiss:hover {
		background: #1f3a1f;
	}

	.apikey-table {
		width: 100%;
		border-collapse: collapse;
		margin-top: 1rem;
	}

	.apikey-table th,
	.apikey-table td {
		padding: 0.6rem 0.8rem;
		text-align: left;
		border-bottom: 1px solid #1e293b;
		font-size: 0.9rem;
	}

	.apikey-table thead th {
		color: #94a3b8;
		font-weight: 500;
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.apikey-table tbody tr:last-child td {
		border-bottom: none;
	}

	.apikey-name {
		font-weight: 600;
		color: #e2e8f0;
	}

	.apikey-table code {
		background: #0f172a;
		padding: 0.15rem 0.4rem;
		border-radius: 3px;
		font-size: 0.85em;
		color: #cbd5e1;
	}

	.apikey-revoke {
		padding: 0.3rem 0.7rem;
		background: transparent;
		border: 1px solid #ef4444;
		border-radius: 4px;
		color: #fca5a5;
		font-size: 0.8rem;
		cursor: pointer;
	}

	.apikey-revoke:hover {
		background: #7f1d1d;
		color: #fecaca;
	}

	main {
		padding: 1.5rem 2rem 60px;
		display: flex;
		flex-direction: column;
		gap: 1.5rem;
	}

	.empty {
		text-align: center;
		padding: 4rem;
		color: #94a3b8;
	}

	.market-section {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.market-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.market-label {
		font-size: 1.1rem;
		font-weight: 700;
	}

	.btn-sm {
		padding: 0.25rem 0.75rem;
		font-size: 0.75rem;
	}

	.gex-grid {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}

	.gex-empty {
		padding: 0.75rem 1rem;
		background: #1e293b;
		border-radius: 8px;
		border: 1px solid #334155;
		font-size: 0.8125rem;
		color: #64748b;
	}

	.gex-loading {
		color: #94a3b8;
	}

	.gex-hint {
		color: #64748b;
	}

	.gex-error {
		color: #f87171;
	}

	.underlying-gex {
		padding: 0.5rem 1.5rem;
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

	.portfolio-left {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 1rem;
	}

	.filter-bar {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	.filter-toggle {
		cursor: pointer;
		position: relative;
		display: inline-block;
	}

	.filter-toggle input {
		position: absolute;
		opacity: 0;
		pointer-events: none;
		width: 0;
		height: 0;
	}

	.filter-pill {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
		color: #94a3b8;
		font-size: 0.85rem;
		transition: color 0.15s;
		user-select: none;
	}

	.filter-check {
		width: 1.1rem;
		height: 1.1rem;
		border-radius: 4px;
		border: 1.5px solid #475569;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		font-size: 0.75rem;
		color: transparent;
		background: transparent;
		transition: background 0.15s, border-color 0.15s, color 0.15s;
	}

	.filter-toggle:hover .filter-check {
		border-color: #64748b;
	}

	.filter-toggle input:checked + .filter-pill {
		color: #dbeafe;
	}

	.filter-toggle input:checked + .filter-pill .filter-check {
		background: #3b82f6;
		border-color: #3b82f6;
		color: white;
	}

	.filter-toggle input:focus-visible + .filter-pill {
		outline: 2px solid #60a5fa;
		outline-offset: 3px;
		border-radius: 4px;
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
