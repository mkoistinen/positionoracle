<script lang="ts">
	import type { Snippet } from 'svelte';

	let { children }: { children: Snippet } = $props();
</script>

<svelte:head>
	<title>PositionOracle</title>
</svelte:head>

<div class="app">
	{@render children()}
</div>

<style>
	:global(*, *::before, *::after) {
		box-sizing: border-box;
	}

	:global(body) {
		margin: 0;
		font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen,
			Ubuntu, Cantarell, 'Open Sans', 'Helvetica Neue', sans-serif;
		background: #0f172a;
		color: #e2e8f0;
	}

	.app {
		min-height: 100vh;
	}

	/* Tippy.js custom theme */
	:global(.tippy-box[data-theme~='positionoracle']) {
		background: #334155;
		color: #e2e8f0;
		border: none;
		border-radius: 8px;
		font-size: 0.8125rem;
		line-height: 1.5;
		box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
	}

	:global(.tippy-box[data-theme~='positionoracle'] .tippy-content) {
		padding: 0.5rem 0.75rem;
	}

	:global(.tippy-box[data-theme~='positionoracle'] .tippy-arrow) {
		color: #334155;
	}
</style>
