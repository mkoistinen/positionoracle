<script lang="ts">
	import { goto } from '$app/navigation';
	import {
		getAuthStatus,
		beginRegistration,
		completeRegistration,
		beginLogin,
		completeLogin
	} from '$lib/api';
	import { prepareCreationOptions, prepareRequestOptions, serializeCredential } from '$lib/webauthn';

	let hasCredentials = $state(false);
	let setupToken = $state('');
	let keyName = $state('');
	let error = $state('');
	let loading = $state(true);
	let working = $state(false);

	$effect(() => {
		getAuthStatus().then((status) => {
			if (status.authenticated) {
				goto('/');
				return;
			}
			hasCredentials = status.has_credentials;
			loading = false;
		});
	});

	async function handleRegister() {
		error = '';
		working = true;

		try {
			const token = setupToken || undefined;
			const { options, challenge_token } = await beginRegistration(token);

			const publicKeyOptions = prepareCreationOptions(options);
			const credential = await navigator.credentials.create({
				publicKey: publicKeyOptions
			}) as PublicKeyCredential | null;

			if (!credential) {
				error = 'Registration was cancelled';
				return;
			}

			const serialized = serializeCredential(credential);
			await completeRegistration(serialized, challenge_token, keyName || 'Default Key');
			goto('/');
		} catch (e: any) {
			error = e.message || 'Registration failed';
		} finally {
			working = false;
		}
	}

	async function handleLogin() {
		error = '';
		working = true;

		try {
			const { options, challenge_token } = await beginLogin();
			const publicKeyOptions = prepareRequestOptions(options);
			const credential = await navigator.credentials.get({
				publicKey: publicKeyOptions
			}) as PublicKeyCredential | null;

			if (!credential) {
				error = 'Authentication was cancelled';
				return;
			}

			const serialized = serializeCredential(credential);
			await completeLogin(serialized, challenge_token);
			goto('/');
		} catch (e: any) {
			error = e.message || 'Authentication failed';
		} finally {
			working = false;
		}
	}
</script>

<div class="center">
	<div class="login-card">
		<h1>PositionOracle</h1>

		{#if loading}
			<p>Loading...</p>
		{:else if hasCredentials}
			<p>Sign in with your passkey</p>
			<button class="btn" onclick={handleLogin} disabled={working}>
				{working ? 'Authenticating...' : 'Sign In'}
			</button>

			<hr />
			<p class="muted">Or register a new passkey</p>
			<div class="form-group">
				<input type="text" bind:value={setupToken} placeholder="Setup token" />
			</div>
			<div class="form-group">
				<input type="text" bind:value={keyName} placeholder="Key name (e.g. MacBook)" />
			</div>
			<button class="btn btn-secondary" onclick={handleRegister} disabled={working || !setupToken}>
				Register New Key
			</button>
		{:else}
			<p>First-time setup: register your passkey</p>
			<div class="form-group">
				<input type="text" bind:value={setupToken} placeholder="Setup token" />
			</div>
			<div class="form-group">
				<input type="text" bind:value={keyName} placeholder="Key name (e.g. MacBook)" />
			</div>
			<button class="btn" onclick={handleRegister} disabled={working || !setupToken}>
				{working ? 'Registering...' : 'Register Passkey'}
			</button>
		{/if}

		{#if error}
			<p class="error">{error}</p>
		{/if}
	</div>
</div>

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
		width: 100%;
	}

	.login-card h1 {
		margin: 0 0 1rem;
		font-size: 2rem;
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
		width: 100%;
		transition: background 0.2s;
	}

	.btn:hover:not(:disabled) {
		background: #2563eb;
	}

	.btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-secondary {
		background: #475569;
	}

	.btn-secondary:hover:not(:disabled) {
		background: #64748b;
	}

	.form-group {
		margin-bottom: 1rem;
	}

	input {
		width: 100%;
		padding: 0.75rem;
		border: 1px solid #475569;
		border-radius: 8px;
		background: #0f172a;
		color: #e2e8f0;
		font-size: 1rem;
	}

	input::placeholder {
		color: #64748b;
	}

	hr {
		border: none;
		border-top: 1px solid #334155;
		margin: 1.5rem 0;
	}

	.muted {
		color: #94a3b8;
		font-size: 0.875rem;
	}

	.error {
		color: #fca5a5;
		margin-top: 1rem;
	}
</style>
