<script lang="ts">
	import { onMount } from 'svelte';
	import * as d3 from 'd3';
	import type { GEXProfile } from './ws';
	import { tooltip } from './tooltip';

	let { profile, compact = false }: { profile: GEXProfile; compact?: boolean } = $props();

	let container: HTMLDivElement;

	function render() {
		if (!container || !profile?.strikes?.length) return;

		const strikes = profile.strikes;
		const chartHeight = compact ? 48 : 64;
		const margin = { top: 4, right: 8, bottom: 16, left: 8 };
		const width = container.clientWidth;
		const innerWidth = width - margin.left - margin.right;
		const innerHeight = chartHeight - margin.top - margin.bottom;

		// Clear previous
		d3.select(container).selectAll('*').remove();

		const svg = d3.select(container)
			.append('svg')
			.attr('width', width)
			.attr('height', chartHeight);

		const g = svg.append('g')
			.attr('transform', `translate(${margin.left},${margin.top})`);

		// Scales
		const xExtent = d3.extent(strikes, d => d.strike) as [number, number];
		const x = d3.scaleLinear()
			.domain(xExtent)
			.range([0, innerWidth]);

		const maxGex = d3.max(strikes, d => Math.max(Math.abs(d.call_gex), Math.abs(d.put_gex))) || 1;
		const y = d3.scaleLinear()
			.domain([-maxGex, maxGex])
			.range([innerHeight, 0]);

		const barWidth = Math.max(1, (innerWidth / strikes.length) * 0.35);
		const zero = y(0);

		// Call GEX bars (positive, above zero line)
		g.selectAll('.bar-call')
			.data(strikes.filter(d => d.call_gex !== 0))
			.enter()
			.append('rect')
			.attr('class', 'bar-call')
			.attr('x', d => x(d.strike) - barWidth)
			.attr('y', d => Math.min(zero, y(d.call_gex)))
			.attr('width', barWidth)
			.attr('height', d => Math.abs(y(d.call_gex) - zero))
			.attr('fill', '#4ade80')
			.attr('opacity', 0.8);

		// Put GEX bars (negative, below zero line)
		g.selectAll('.bar-put')
			.data(strikes.filter(d => d.put_gex !== 0))
			.enter()
			.append('rect')
			.attr('class', 'bar-put')
			.attr('x', d => x(d.strike))
			.attr('y', d => Math.min(zero, y(d.put_gex)))
			.attr('width', barWidth)
			.attr('height', d => Math.abs(y(d.put_gex) - zero))
			.attr('fill', '#f87171')
			.attr('opacity', 0.8);

		// Zero line
		g.append('line')
			.attr('x1', 0)
			.attr('x2', innerWidth)
			.attr('y1', zero)
			.attr('y2', zero)
			.attr('stroke', '#475569')
			.attr('stroke-width', 1);

		// Spot price line
		if (profile.spot_price >= xExtent[0] && profile.spot_price <= xExtent[1]) {
			g.append('line')
				.attr('x1', x(profile.spot_price))
				.attr('x2', x(profile.spot_price))
				.attr('y1', 0)
				.attr('y2', innerHeight)
				.attr('stroke', '#e2e8f0')
				.attr('stroke-width', 1.5)
				.attr('stroke-dasharray', '3,2');
		}

		// Call Wall marker
		if (profile.call_wall >= xExtent[0] && profile.call_wall <= xExtent[1]) {
			g.append('line')
				.attr('x1', x(profile.call_wall))
				.attr('x2', x(profile.call_wall))
				.attr('y1', 0)
				.attr('y2', innerHeight)
				.attr('stroke', '#4ade80')
				.attr('stroke-width', 1.5)
				.attr('stroke-dasharray', '6,3');

			g.append('text')
				.attr('x', x(profile.call_wall))
				.attr('y', margin.top)
				.attr('text-anchor', 'middle')
				.attr('fill', '#4ade80')
				.attr('font-size', '9px')
				.text('CW');
		}

		// Put Wall marker
		if (profile.put_wall >= xExtent[0] && profile.put_wall <= xExtent[1]) {
			g.append('line')
				.attr('x1', x(profile.put_wall))
				.attr('x2', x(profile.put_wall))
				.attr('y1', 0)
				.attr('y2', innerHeight)
				.attr('stroke', '#f87171')
				.attr('stroke-width', 1.5)
				.attr('stroke-dasharray', '6,3');

			g.append('text')
				.attr('x', x(profile.put_wall))
				.attr('y', margin.top)
				.attr('text-anchor', 'middle')
				.attr('fill', '#f87171')
				.attr('font-size', '9px')
				.text('PW');
		}

		// Flip point marker
		if (profile.flip_point >= xExtent[0] && profile.flip_point <= xExtent[1] &&
			profile.flip_point !== profile.spot_price) {
			g.append('line')
				.attr('x1', x(profile.flip_point))
				.attr('x2', x(profile.flip_point))
				.attr('y1', 0)
				.attr('y2', innerHeight)
				.attr('stroke', '#fbbf24')
				.attr('stroke-width', 1)
				.attr('stroke-dasharray', '2,2');
		}

		// Strike labels on x-axis (sparse)
		const labelStrides = Math.max(1, Math.floor(strikes.length / 8));
		const labelStrikes = strikes.filter((_, i) => i % labelStrides === 0);

		g.selectAll('.strike-label')
			.data(labelStrikes)
			.enter()
			.append('text')
			.attr('class', 'strike-label')
			.attr('x', d => x(d.strike))
			.attr('y', innerHeight + 12)
			.attr('text-anchor', 'middle')
			.attr('fill', '#64748b')
			.attr('font-size', '8px')
			.text(d => d.strike.toFixed(0));
	}

	onMount(() => {
		render();
		const observer = new ResizeObserver(() => render());
		observer.observe(container);
		return () => observer.disconnect();
	});

	$effect(() => {
		// Re-render when profile changes
		if (profile) render();
	});
</script>

<div class="gex-chart-wrapper">
	<div class="gex-chart-header">
		<span class="gex-ticker">{profile.underlying}</span>
		<span class="gex-spot">{profile.spot_price.toFixed(2)}</span>
		<div class="gex-stats">
			<span
				class="gex-stat"
				class:gex-positive={profile.net_gex > 0}
				class:gex-negative={profile.net_gex < 0}
				use:tooltip={profile.net_gex > 0
					? 'Positive GEX — dealer hedging dampens moves (low vol regime)'
					: 'Negative GEX — dealer hedging amplifies moves (high vol regime)'}
			>
				GEX {profile.net_gex > 0 ? '+' : ''}{(profile.net_gex / 1e6).toFixed(1)}M
			</span>
			<span class="gex-stat gex-call-wall" use:tooltip={`Call Wall at $${profile.call_wall.toFixed(0)} — resistance from dealer hedging`}>
				CW {profile.call_wall.toFixed(0)}
			</span>
			<span class="gex-stat gex-put-wall" use:tooltip={`Put Wall at $${profile.put_wall.toFixed(0)} — support from dealer hedging`}>
				PW {profile.put_wall.toFixed(0)}
			</span>
			<span class="gex-stat gex-flip" use:tooltip={`Flip point at $${profile.flip_point.toFixed(0)} ��� GEX regime changes here`}>
				Flip {profile.flip_point.toFixed(0)}
			</span>
		</div>
	</div>
	<div class="gex-chart" bind:this={container}></div>
</div>

<style>
	.gex-chart-wrapper {
		background: #1e293b;
		border-radius: 8px;
		padding: 0.5rem 1rem;
		border: 1px solid #334155;
	}

	.gex-chart-header {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 0.25rem;
	}

	.gex-ticker {
		font-weight: 700;
		font-size: 0.8125rem;
	}

	.gex-spot {
		font-size: 0.8125rem;
		font-family: 'SF Mono', 'Fira Code', monospace;
		color: #94a3b8;
	}

	.gex-stats {
		display: flex;
		flex-wrap: wrap;
		gap: 0.375rem;
		margin-left: auto;
	}

	.gex-stat {
		background: #334155;
		padding: 0.125rem 0.5rem;
		border-radius: 4px;
		font-size: 0.6875rem;
		font-family: 'SF Mono', 'Fira Code', monospace;
		color: #94a3b8;
	}

	.gex-positive {
		color: #4ade80;
	}

	.gex-negative {
		color: #f87171;
	}

	.gex-call-wall {
		color: #4ade80;
	}

	.gex-put-wall {
		color: #f87171;
	}

	.gex-flip {
		color: #fbbf24;
	}

	.gex-chart {
		width: 100%;
		min-height: 48px;
	}
</style>
