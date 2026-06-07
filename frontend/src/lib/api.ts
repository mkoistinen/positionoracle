/**
 * API client for PositionOracle backend.
 */

export interface AuthStatus {
	authenticated: boolean;
	has_credentials: boolean;
}

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
	const response = await fetch(url, {
		...options,
		credentials: 'same-origin'
	});
	if (response.status === 401) {
		window.dispatchEvent(new Event('po:unauthorized'));
	}
	if (!response.ok) {
		const text = await response.text();
		throw new Error(`${response.status}: ${text}`);
	}
	return response.json();
}

export async function getAuthStatus(): Promise<AuthStatus> {
	return fetchJson('/api/auth/status');
}

export async function beginRegistration(setupToken?: string): Promise<{ options: PublicKeyCredentialCreationOptions; challenge_token: string }> {
	const params = setupToken ? `?setup_token=${encodeURIComponent(setupToken)}` : '';
	return fetchJson(`/api/auth/register/begin${params}`, { method: 'POST' });
}

export async function completeRegistration(credential: object, challengeToken: string, name: string): Promise<void> {
	await fetchJson('/api/auth/register/complete', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ credential, challenge_token: challengeToken, name })
	});
}

export async function beginLogin(): Promise<{ options: PublicKeyCredentialRequestOptions; challenge_token: string }> {
	return fetchJson('/api/auth/login/begin', { method: 'POST' });
}

export async function completeLogin(credential: object, challengeToken: string): Promise<void> {
	await fetchJson('/api/auth/login/complete', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ credential, challenge_token: challengeToken })
	});
}

export async function logout(): Promise<void> {
	await fetchJson('/api/auth/logout', { method: 'POST' });
}

export async function importPositions(file: File): Promise<{ imported: number }> {
	const formData = new FormData();
	formData.append('file', file);
	return fetchJson('/api/positions/import', {
		method: 'POST',
		body: formData
	});
}

export interface FlexFetchResult {
	imported: number;
	cached: boolean;
	stale: boolean;
	report_generated_at: string | null;
	last_attempt_at: string | null;
	error: string | null;
}

export async function fetchPositionsFromIB(force: boolean = false): Promise<FlexFetchResult> {
	const params = force ? '?force=true' : '';
	return fetchJson(`/api/positions/fetch${params}`, { method: 'POST' });
}

export async function analyzeSymbol(underlying: string): Promise<{ analysis: string }> {
	return fetchJson(`/api/analyze/${encodeURIComponent(underlying)}`, { method: 'POST' });
}

export async function refreshGex(): Promise<{ status: string; profiles: string[] }> {
	return fetchJson('/api/gex/refresh', { method: 'POST' });
}

export interface BlacklistEntry {
	symbol: string;
	loss_date: string;
	expires: string;
	days_remaining: number;
}

export interface BlacklistResponse {
	entries: BlacklistEntry[];
	last_report_generated: string | null;
}

export async function getBlacklist(): Promise<BlacklistResponse> {
	return fetchJson('/api/washsale/blacklist');
}

// ---------------------------------------------------------------------------
// API key management
// ---------------------------------------------------------------------------

export interface ApiKeyListItem {
	id: number;
	name: string;
	key_prefix: string;
	created_at: string;
	last_used_at: string | null;
}

export interface ApiKeyCreated extends ApiKeyListItem {
	/** The cleartext API key. Shown ONCE — store it immediately. */
	key: string;
}

export async function listApiKeys(): Promise<{ keys: ApiKeyListItem[] }> {
	return fetchJson('/api/keys');
}

export async function createApiKey(name: string): Promise<ApiKeyCreated> {
	return fetchJson('/api/keys', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ name })
	});
}

export async function deleteApiKey(id: number): Promise<void> {
	await fetchJson(`/api/keys/${id}`, { method: 'DELETE' });
}

// ---------------------------------------------------------------------------
// OAuth client management (session-authenticated)
// ---------------------------------------------------------------------------

/** One entry from `GET /api/oauth/clients`. No secrets are returned. */
export interface OAuthClientItem {
	client_id: string;
	name: string;
	is_public: boolean;
	client_secret_prefix: string | null;
	redirect_uris: string[];
	scope: string;
	created_at: string;
	last_used_at: string | null;
}

/** Response from `POST /api/oauth/clients`. The cleartext secret is shown ONCE. */
export interface OAuthClientCreated {
	client_id: string;
	client_secret: string;
	client_secret_prefix: string;
	name: string;
	is_public: boolean;
	created_at: string;
}

export async function listOAuthClients(): Promise<{ clients: OAuthClientItem[] }> {
	return fetchJson('/api/oauth/clients');
}

export async function createOAuthClient(name: string): Promise<OAuthClientCreated> {
	return fetchJson('/api/oauth/clients', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ name })
	});
}

export async function deleteOAuthClient(clientId: string): Promise<void> {
	await fetchJson(`/api/oauth/clients/${encodeURIComponent(clientId)}`, {
		method: 'DELETE'
	});
}

export async function updateOAuthClientRedirectUris(
	clientId: string,
	redirectUris: string[]
): Promise<{ redirect_uris: string[] }> {
	return fetchJson(`/api/oauth/clients/${encodeURIComponent(clientId)}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ redirect_uris: redirectUris })
	});
}
