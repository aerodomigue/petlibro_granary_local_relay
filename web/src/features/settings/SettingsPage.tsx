import type { JSX } from "react";

import { usePreferences } from "../../preferences/PreferencesContext";

/** Render browser-wide preferences separately from device-specific controls. */
export function SettingsPage(): JSX.Element {
  const { advancedMode, setAdvancedMode } = usePreferences();
  return <section aria-labelledby="settings-title" className="settings-page">
    <header className="page-heading"><div><h1 id="settings-title">Settings</h1><p>Preferences for this dashboard.</p></div></header>
    <article className="settings-card"><h2>Advanced mode</h2><p>Show technical diagnostics for troubleshooting. Sensitive credentials are never displayed.</p><label className="switch-row" htmlFor="advanced-mode"><span><strong>Advanced diagnostics</strong><small>Device IDs, relay state and technical logs.</small></span><input checked={advancedMode} id="advanced-mode" onChange={(event) => setAdvancedMode(event.target.checked)} role="switch" type="checkbox" /></label></article>
  </section>;
}
