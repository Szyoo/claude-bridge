// Top-right corner tools of the site pages: @szyyw/design's mountChrome, loaded from design.szyyw.xyz through the
// page's import map ("@szyyw/design/" → DESIGN_BASE, version = DESIGN_VERSION in standalone.py, rendered by the
// server; scripts/update-design.sh bumps it) — 🌗 + appearance panel always, plus the app
// switcher and account menu under portal SSO (<html data-sso="1" data-portal="…">, set by the server).
// The pages load tokens.css + components.css themselves; the server renders <html data-theme/-palette/-scheme> from the
// cb_theme / cb_palette / cb_scheme cookies this writes, so the first paint is already right.
// The embedded widget (create_bridge + bridge-widget.*) never loads this.
import { mountChrome } from '@szyyw/design/chrome.js';

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
