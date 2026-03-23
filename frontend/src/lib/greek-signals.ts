/**
 * Color-coding logic for option Greeks based on position direction.
 *
 * Each Greek is evaluated in the context of the position (long/short,
 * call/put) to determine whether it's working for or against the holder.
 *
 * Levels:
 *   fantastic (blue)  — strongly favorable
 *   ok (green)        — neutral or mildly favorable
 *   warning (yellow)  — unfavorable, consider monitoring
 *   danger (red)      — actively working against you, consider action
 *
 * Units:
 *   Delta  — per contract (0 to ±1)
 *   Theta  — $/day per contract
 *   Vega   — $ per 1% IV change per contract
 *   Gamma  — delta change per $1 underlying move per contract
 *   Vanna  — delta change per 1% IV change per contract
 *   Charm  — delta change per day per contract
 *   Vomma  — vega change per 1% IV change per contract
 */

import type { PositionData } from './ws';

export type SignalLevel = 'fantastic' | 'ok' | 'warning' | 'danger';

export interface GreekSignal {
	level: SignalLevel;
	reason: string;
}

/** True if the position is net short. */
function isShort(pos: PositionData): boolean {
	return pos.quantity < 0;
}

/** Days to expiration from today. */
function dte(pos: PositionData): number {
	const exp = new Date(pos.expiration + 'T00:00:00');
	const now = new Date();
	return Math.max(0, Math.ceil((exp.getTime() - now.getTime()) / 86400000));
}

export function evaluateDelta(pos: PositionData): GreekSignal {
	const d = pos.greeks.delta;
	const absDelta = Math.abs(d);
	const short = isShort(pos);

	if (absDelta === 0) return { level: 'ok', reason: 'Delta 0 per contract — no directional exposure.' };

	if (short) {
		if (absDelta < 0.15) return { level: 'fantastic', reason: `Delta ${d.toFixed(3)} per contract — deep OTM, very unlikely to be exercised.` };
		if (absDelta < 0.30) return { level: 'ok', reason: `Delta ${d.toFixed(3)} per contract — comfortably OTM.` };
		if (absDelta < 0.50) return { level: 'warning', reason: `Delta ${d.toFixed(3)} per contract — approaching ATM, directional risk increasing.` };
		return { level: 'danger', reason: `Delta ${d.toFixed(3)} per contract — ITM or near ATM. High assignment risk.` };
	} else {
		if (absDelta > 0.70) return { level: 'fantastic', reason: `Delta ${d.toFixed(3)} per contract — deep ITM, moving with underlying.` };
		if (absDelta > 0.40) return { level: 'ok', reason: `Delta ${d.toFixed(3)} per contract — moderate directional exposure.` };
		if (absDelta > 0.20) return { level: 'warning', reason: `Delta ${d.toFixed(3)} per contract — OTM, less responsive to underlying moves.` };
		return { level: 'danger', reason: `Delta ${d.toFixed(3)} per contract — deep OTM, unlikely to profit from direction.` };
	}
}

export function evaluateTheta(pos: PositionData): GreekSignal {
	const theta = pos.greeks.theta;
	const short = isShort(pos);

	if (theta === 0) return { level: 'ok', reason: 'Theta $0/day per contract — no time decay effect.' };

	if (short) {
		if (theta < -0.05) return { level: 'fantastic', reason: `Theta $${theta.toFixed(4)}/day per contract — rapid time decay is earning you money.` };
		if (theta < -0.01) return { level: 'ok', reason: `Theta $${theta.toFixed(4)}/day per contract — steady time decay in your favor.` };
		return { level: 'ok', reason: `Theta $${theta.toFixed(4)}/day per contract — minimal time decay.` };
	} else {
		if (theta < -0.05) return { level: 'danger', reason: `Theta $${theta.toFixed(4)}/day per contract — losing significant value daily to time decay.` };
		if (theta < -0.02) return { level: 'warning', reason: `Theta $${theta.toFixed(4)}/day per contract — noticeable daily time decay erosion.` };
		return { level: 'ok', reason: `Theta $${theta.toFixed(4)}/day per contract — minimal time decay impact.` };
	}
}

export function evaluateVega(pos: PositionData): GreekSignal {
	const vega = pos.greeks.vega;
	const short = isShort(pos);
	const absVega = Math.abs(vega);

	if (absVega === 0) return { level: 'ok', reason: 'Vega $0 per 1% IV change — no volatility exposure.' };

	if (short) {
		if (absVega > 0.20) return { level: 'danger', reason: `Vega $${vega.toFixed(4)} per 1% IV change — very sensitive. A vol spike will hurt.` };
		if (absVega > 0.10) return { level: 'warning', reason: `Vega $${vega.toFixed(4)} per 1% IV change — moderate vol exposure. Watch for events.` };
		if (absVega > 0.03) return { level: 'ok', reason: `Vega $${vega.toFixed(4)} per 1% IV change — manageable vol exposure.` };
		return { level: 'fantastic', reason: `Vega $${vega.toFixed(4)} per 1% IV change — minimal vol exposure. Safe from IV spikes.` };
	} else {
		if (absVega > 0.10) return { level: 'fantastic', reason: `Vega $${vega.toFixed(4)} per 1% IV change — well positioned for a vol expansion.` };
		if (absVega > 0.03) return { level: 'ok', reason: `Vega $${vega.toFixed(4)} per 1% IV change — moderate vol sensitivity.` };
		return { level: 'warning', reason: `Vega $${vega.toFixed(4)} per 1% IV change — low vol sensitivity, won't benefit much from IV increase.` };
	}
}

export function evaluateGamma(pos: PositionData): GreekSignal {
	const gamma = pos.greeks.gamma;
	const short = isShort(pos);
	const daysLeft = dte(pos);

	if (gamma === 0) return { level: 'ok', reason: 'Gamma 0 per $1 move — delta is stable.' };

	if (short) {
		if (gamma > 0.10 && daysLeft <= 7) return { level: 'danger', reason: `Gamma ${gamma.toFixed(4)} delta/$1 move with ${daysLeft} DTE — extreme pin risk. Delta will swing violently.` };
		if (gamma > 0.05 && daysLeft <= 14) return { level: 'warning', reason: `Gamma ${gamma.toFixed(4)} delta/$1 move with ${daysLeft} DTE — elevated gamma risk as expiry approaches.` };
		if (gamma > 0.05) return { level: 'warning', reason: `Gamma ${gamma.toFixed(4)} delta/$1 move — delta is unstable, underlying moves will shift your exposure.` };
		return { level: 'ok', reason: `Gamma ${gamma.toFixed(4)} delta/$1 move — delta is relatively stable.` };
	} else {
		if (gamma > 0.05) return { level: 'fantastic', reason: `Gamma ${gamma.toFixed(4)} delta/$1 move — strong convexity. Big moves amplify your profit.` };
		if (gamma > 0.02) return { level: 'ok', reason: `Gamma ${gamma.toFixed(4)} delta/$1 move — moderate convexity benefit.` };
		return { level: 'ok', reason: `Gamma ${gamma.toFixed(4)} delta/$1 move — low convexity.` };
	}
}

export function evaluateVanna(pos: PositionData): GreekSignal {
	const vanna = pos.greeks.vanna;
	const short = isShort(pos);
	const absVanna = Math.abs(vanna);

	if (absVanna === 0) return { level: 'ok', reason: 'Vanna 0 delta per 1% IV change — delta is stable against vol changes.' };
	if (absVanna < 0.005) return { level: 'ok', reason: `Vanna ${vanna.toFixed(4)} delta per 1% IV change — negligible. Delta won't shift much with IV changes.` };

	if (short) {
		const deltaShiftBad = (pos.contract_type === 'put' && vanna > 0) || (pos.contract_type === 'call' && vanna < 0);

		if (deltaShiftBad && absVanna > 0.03) return { level: 'danger', reason: `Vanna ${vanna.toFixed(4)} delta per 1% IV change — a vol spike will push delta toward ITM. Double whammy risk.` };
		if (deltaShiftBad && absVanna > 0.01) return { level: 'warning', reason: `Vanna ${vanna.toFixed(4)} delta per 1% IV change — vol increase will shift delta unfavorably.` };
		if (!deltaShiftBad && absVanna > 0.01) return { level: 'fantastic', reason: `Vanna ${vanna.toFixed(4)} delta per 1% IV change — a vol spike would push delta away from ITM. Natural hedge.` };
		return { level: 'ok', reason: `Vanna ${vanna.toFixed(4)} delta per 1% IV change — minor delta sensitivity to vol.` };
	} else {
		if (absVanna > 0.03) return { level: 'warning', reason: `Vanna ${vanna.toFixed(4)} delta per 1% IV change — delta is highly sensitive to IV changes.` };
		return { level: 'ok', reason: `Vanna ${vanna.toFixed(4)} delta per 1% IV change — moderate delta/vol interaction.` };
	}
}

export function evaluateCharm(pos: PositionData): GreekSignal {
	const charm = pos.greeks.charm;
	const delta = pos.greeks.delta;
	const short = isShort(pos);
	const absCharm = Math.abs(charm);
	const daysLeft = dte(pos);

	if (absCharm === 0) return { level: 'ok', reason: 'Charm 0 delta/day — delta is stable over time.' };
	if (absCharm < 0.002) return { level: 'ok', reason: `Charm ${charm.toFixed(4)} delta/day — negligible daily delta drift.` };

	if (short) {
		const deltaDecreasing = (delta > 0 && charm < 0) || (delta < 0 && charm > 0);

		if (deltaDecreasing) {
			if (absCharm > 0.01) return { level: 'fantastic', reason: `Charm ${charm.toFixed(4)} delta/day — delta decaying rapidly toward zero. Time is strongly on your side.` };
			return { level: 'ok', reason: `Charm ${charm.toFixed(4)} delta/day — delta drifting favorably toward zero.` };
		} else {
			if (absCharm > 0.01 && daysLeft <= 14) return { level: 'danger', reason: `Charm ${charm.toFixed(4)} delta/day — delta accelerating away from zero with ${daysLeft} DTE. Roll or close.` };
			if (absCharm > 0.005) return { level: 'warning', reason: `Charm ${charm.toFixed(4)} delta/day — delta drifting unfavorably. Monitor closely.` };
			return { level: 'ok', reason: `Charm ${charm.toFixed(4)} delta/day — slight unfavorable delta drift.` };
		}
	} else {
		if (absCharm > 0.01) return { level: 'warning', reason: `Charm ${charm.toFixed(4)} delta/day — delta shifting significantly each day.` };
		return { level: 'ok', reason: `Charm ${charm.toFixed(4)} delta/day — moderate daily delta drift.` };
	}
}

export function evaluateVomma(pos: PositionData): GreekSignal {
	const vomma = pos.greeks.vomma;
	const short = isShort(pos);
	const absVomma = Math.abs(vomma);

	if (absVomma === 0) return { level: 'ok', reason: 'Vomma $0 vega per 1% IV change — vega is stable against vol changes.' };
	if (absVomma < 0.005) return { level: 'ok', reason: `Vomma $${vomma.toFixed(4)} vega per 1% IV change — negligible. Vega exposure stable.` };

	if (short) {
		if (absVomma > 0.05) return { level: 'danger', reason: `Vomma $${vomma.toFixed(4)} vega per 1% IV change — vol spike will amplify your vega exposure. Cascading risk.` };
		if (absVomma > 0.02) return { level: 'warning', reason: `Vomma $${vomma.toFixed(4)} vega per 1% IV change — vega exposure will increase if vol rises.` };
		return { level: 'ok', reason: `Vomma $${vomma.toFixed(4)} vega per 1% IV change — vega exposure relatively stable.` };
	} else {
		if (absVomma > 0.02) return { level: 'fantastic', reason: `Vomma $${vomma.toFixed(4)} vega per 1% IV change — vol spike will amplify your vega benefit. Convexity in vol.` };
		return { level: 'ok', reason: `Vomma $${vomma.toFixed(4)} vega per 1% IV change — moderate vol convexity.` };
	}
}

const STOCK_NEUTRAL: GreekSignal = { level: 'ok', reason: 'Not applicable to stock positions.' };

/** Evaluate all Greeks for a position. */
export function evaluateAll(pos: PositionData): Record<string, GreekSignal> {
	if (pos.contract_type === 'stock') {
		const deltaSignal: GreekSignal = {
			level: 'ok',
			reason: `Stock: ${pos.quantity} shares = ${pos.quantity} delta (1.0 delta per share).`,
		};
		return {
			delta: deltaSignal,
			theta: STOCK_NEUTRAL,
			vega: STOCK_NEUTRAL,
			gamma: STOCK_NEUTRAL,
			vanna: STOCK_NEUTRAL,
			charm: STOCK_NEUTRAL,
			vomma: STOCK_NEUTRAL,
		};
	}

	return {
		delta: evaluateDelta(pos),
		theta: evaluateTheta(pos),
		vega: evaluateVega(pos),
		gamma: evaluateGamma(pos),
		vanna: evaluateVanna(pos),
		charm: evaluateCharm(pos),
		vomma: evaluateVomma(pos),
	};
}

/** Evaluate net delta for the per-underlying rollup. */
export function evaluateNetDelta(netDelta: number): GreekSignal {
	const abs = Math.abs(netDelta);
	if (abs < 10) return { level: 'fantastic', reason: `Net delta ${netDelta.toFixed(1)} shares-equivalent — near delta-neutral. Minimal directional risk.` };
	if (abs < 50) return { level: 'ok', reason: `Net delta ${netDelta.toFixed(1)} shares-equivalent — moderate directional exposure.` };
	if (abs < 150) return { level: 'warning', reason: `Net delta ${netDelta.toFixed(1)} shares-equivalent — significant directional bias. Consider hedging.` };
	return { level: 'danger', reason: `Net delta ${netDelta.toFixed(1)} shares-equivalent — heavy directional exposure. A 1% underlying move ≈ $${(abs * 0.01 * 100).toFixed(0)} P&L swing.` };
}

/** Evaluate net theta for the per-underlying rollup. */
export function evaluateNetTheta(netTheta: number): GreekSignal {
	if (netTheta > 5) return { level: 'fantastic', reason: `Net theta $${netTheta.toFixed(1)}/day — earning ~$${netTheta.toFixed(0)} daily from time decay.` };
	if (netTheta > 0) return { level: 'ok', reason: `Net theta $${netTheta.toFixed(1)}/day — net time decay in your favor.` };
	if (netTheta > -5) return { level: 'warning', reason: `Net theta $${netTheta.toFixed(1)}/day — paying time decay.` };
	return { level: 'danger', reason: `Net theta $${netTheta.toFixed(1)}/day — losing ~$${Math.abs(netTheta).toFixed(0)} daily to time decay.` };
}

/** Evaluate net vega for the per-underlying rollup. */
export function evaluateNetVega(netVega: number): GreekSignal {
	const abs = Math.abs(netVega);
	if (abs < 5) return { level: 'fantastic', reason: `Net vega $${netVega.toFixed(1)} per 1% IV change — minimal volatility exposure.` };
	if (abs < 20) return { level: 'ok', reason: `Net vega $${netVega.toFixed(1)} per 1% IV change — moderate vol sensitivity.` };
	if (abs < 50) return { level: 'warning', reason: `Net vega $${netVega.toFixed(1)} per 1% IV change — a 1% IV change ≈ $${abs.toFixed(0)} P&L impact.` };
	return { level: 'danger', reason: `Net vega $${netVega.toFixed(1)} per 1% IV change — extreme vol exposure. ≈ $${abs.toFixed(0)} P&L per 1% IV move.` };
}

/** Evaluate net gamma for the per-underlying rollup. */
export function evaluateNetGamma(netGamma: number): GreekSignal {
	const abs = Math.abs(netGamma);
	if (abs < 1) return { level: 'fantastic', reason: `Net gamma ${netGamma.toFixed(1)} delta per $1 move — delta is stable across underlying moves.` };
	if (abs < 5) return { level: 'ok', reason: `Net gamma ${netGamma.toFixed(1)} delta per $1 move — moderate delta sensitivity.` };
	if (abs < 15) return { level: 'warning', reason: `Net gamma ${netGamma.toFixed(1)} delta per $1 move — delta shifts meaningfully per $1 move.` };
	return { level: 'danger', reason: `Net gamma ${netGamma.toFixed(1)} delta per $1 move — extreme delta instability. A $1 move changes delta by ${abs.toFixed(0)}.` };
}

/** Evaluate beta-weighted delta (SPY-equivalent shares). */
export function evaluateBetaWeightedDelta(bwDelta: number, beta?: number): GreekSignal {
	const abs = Math.abs(bwDelta);
	const betaNote = beta != null ? ` (β=${beta.toFixed(2)})` : '';
	if (abs < 5) return { level: 'fantastic', reason: `SPY Δ ${bwDelta.toFixed(1)} SPY-equivalent shares${betaNote} — near market-neutral.` };
	if (abs < 25) return { level: 'ok', reason: `SPY Δ ${bwDelta.toFixed(1)} SPY-equivalent shares${betaNote} — moderate market exposure.` };
	if (abs < 75) return { level: 'warning', reason: `SPY Δ ${bwDelta.toFixed(1)} SPY-equivalent shares${betaNote} — significant market exposure.` };
	return { level: 'danger', reason: `SPY Δ ${bwDelta.toFixed(1)} SPY-equivalent shares${betaNote} — heavy market exposure. A 1% SPY move ≈ $${(abs * 0.01 * 565).toFixed(0)} P&L.` };
}

/** Map signal level to CSS color class. */
export function signalClass(level: SignalLevel): string {
	switch (level) {
		case 'fantastic': return 'signal-fantastic';
		case 'ok': return 'signal-ok';
		case 'warning': return 'signal-warning';
		case 'danger': return 'signal-danger';
	}
}
