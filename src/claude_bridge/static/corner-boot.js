// Top-right corner tools of the `serve --multi-user` pages: @szyyw/design's mountChrome (version in
// vendor/szyyw-design/VENDORED.md, synced by scripts/update-design.sh) — 🌗 + appearance panel always, plus the app
// switcher and account menu under portal SSO (<html data-sso="1" data-portal="…">, set by the server).
// The pages load tokens.css + components.css themselves; the server renders <html data-theme/-palette/-scheme> from the
// cb_theme / cb_palette / cb_scheme cookies this writes, so the first paint is already right.
// Standalone (single password) mode and the embedded widget (create_bridge + bridge-widget.*) never load this.
import { mountChrome } from './vendor/szyyw-design/chrome.js';

const root = document.documentElement;
const sso = root.dataset.sso === '1';
mountChrome({
  background: document.querySelector('.bg-layer'),
  cookiePrefix: 'cb_',
  locale: 'zh',
  portal: sso ? (root.dataset.portal || 'https://szyyw.xyz') : null,
  appearance: {
    dotField: { update: { command: (v) => `bash scripts/update-design.sh v${String(v).replace(/^v/, '')}  # claude-bridge 仓库，然后发版` } },
  },
});
