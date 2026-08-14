"""Dependency-free dashboard markup.

The browser application lives in :mod:`petlibro_relay.web.user_interface` so
the server-facing module remains small and the UI can be reviewed separately.
"""

from __future__ import annotations

from .user_interface import DASHBOARD_CSS, DASHBOARD_JAVASCRIPT


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PETLIBRO Local Relay</title>
  <style>""" + DASHBOARD_CSS + """</style>
</head>
<body>
  <header class="app-header">
    <div>
      <h1>PETLIBRO</h1>
      <p class="app-subtitle">Your feeder, at a glance</p>
    </div>
    <div id="header-status" class="header-status" aria-live="polite">Loading…</div>
  </header>
  <nav id="primary-nav" class="primary-nav" aria-label="Main navigation"></nav>
  <main id="application" class="application"></main>
  <dialog id="app-modal" aria-labelledby="modal-title">
    <div class="modal-shell">
      <div class="modal-heading"><h2 id="modal-title">PETLIBRO</h2><button id="modal-dismiss" class="icon-button" type="button" aria-label="Close">×</button></div>
      <div id="modal-content" class="modal-content"></div>
    </div>
  </dialog>
  <script>""" + DASHBOARD_JAVASCRIPT + """</script>
</body>
</html>"""
