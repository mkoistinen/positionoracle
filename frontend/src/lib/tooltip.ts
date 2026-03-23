/**
 * Svelte action for Tippy.js tooltips with arrow pointers.
 *
 * Usage: <td use:tooltip={"Some tooltip text"}>
 */

import tippy, { type Instance, type Props } from 'tippy.js';
import 'tippy.js/dist/tippy.css';
import 'tippy.js/themes/light-border.css';

const defaultProps: Partial<Props> = {
	arrow: true,
	placement: 'top',
	theme: 'positionoracle',
	delay: [200, 0],
	duration: [200, 150],
	maxWidth: 350,
};

export function tooltip(node: HTMLElement, content: string) {
	let instance: Instance | undefined;

	function create() {
		if (instance) instance.destroy();
		if (!content) return;
		instance = tippy(node, {
			...defaultProps,
			content,
		});
	}

	create();

	return {
		update(newContent: string) {
			content = newContent;
			if (instance) {
				instance.setContent(newContent);
			} else {
				create();
			}
		},
		destroy() {
			instance?.destroy();
		},
	};
}
