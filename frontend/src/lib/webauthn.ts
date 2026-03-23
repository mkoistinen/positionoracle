/**
 * WebAuthn helpers for passkey registration and authentication.
 */

function base64urlToBuffer(base64url: string): ArrayBuffer {
	const base64 = base64url.replace(/-/g, '+').replace(/_/g, '/');
	const padding = '='.repeat((4 - (base64.length % 4)) % 4);
	const binary = atob(base64 + padding);
	const bytes = new Uint8Array(binary.length);
	for (let i = 0; i < binary.length; i++) {
		bytes[i] = binary.charCodeAt(i);
	}
	return bytes.buffer;
}

function bufferToBase64url(buffer: ArrayBuffer): string {
	const bytes = new Uint8Array(buffer);
	let binary = '';
	for (const byte of bytes) {
		binary += String.fromCharCode(byte);
	}
	return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function prepareCreationOptions(options: any): PublicKeyCredentialCreationOptions {
	return {
		...options,
		challenge: base64urlToBuffer(options.challenge),
		user: {
			...options.user,
			id: base64urlToBuffer(options.user.id)
		},
		excludeCredentials: options.excludeCredentials?.map((c: any) => ({
			...c,
			id: base64urlToBuffer(c.id)
		}))
	};
}

export function prepareRequestOptions(options: any): PublicKeyCredentialRequestOptions {
	return {
		...options,
		challenge: base64urlToBuffer(options.challenge),
		allowCredentials: options.allowCredentials?.map((c: any) => ({
			...c,
			id: base64urlToBuffer(c.id)
		}))
	};
}

export function serializeCredential(credential: PublicKeyCredential): object {
	const response = credential.response as AuthenticatorAttestationResponse | AuthenticatorAssertionResponse;
	const result: Record<string, any> = {
		id: credential.id,
		rawId: bufferToBase64url(credential.rawId),
		type: credential.type,
		response: {
			clientDataJSON: bufferToBase64url(response.clientDataJSON)
		}
	};

	if ('attestationObject' in response) {
		result.response.attestationObject = bufferToBase64url(response.attestationObject);
	}
	if ('authenticatorData' in response) {
		result.response.authenticatorData = bufferToBase64url(
			(response as AuthenticatorAssertionResponse).authenticatorData
		);
		result.response.signature = bufferToBase64url(
			(response as AuthenticatorAssertionResponse).signature
		);
		const userHandle = (response as AuthenticatorAssertionResponse).userHandle;
		if (userHandle) {
			result.response.userHandle = bufferToBase64url(userHandle);
		}
	}

	return result;
}
