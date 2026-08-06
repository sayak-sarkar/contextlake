### Fixed
- **Dashboard: primary navigation links keep their accessible name when the rail is
  collapsed, the viewport narrows below 1280px, or the page is zoomed to 200% (WCAG 4.1.2
  Name/Role/Value, 2.4.4 Link Purpose).** Each nav link paired a decorative,
  `aria-hidden` icon with a visible text label, and nothing else -- correct as long as
  the label stayed on screen. All three of those states hide the label with
  `display:none` (a one-click "more screen space" toggle, a normal laptop-width
  viewport, and a standard accessibility accommodation WCAG 1.4.4 exists to require
  support for), which left the icon contributing nothing and the label removed from the
  render tree entirely -- ten unlabelled links in the primary nav, indistinguishable
  from each other to a screen reader. Each link now also carries an `aria-label`
  mirroring its own visible text exactly (the same pattern already used correctly by the
  neighboring "Collapse navigation" button), so an accessible name survives every one of
  those states. Verified with real Chromium accessibility-tree snapshots at each
  trigger, not by reading the CSS: all ten links are named at the desktop width, all ten
  stay named immediately after collapsing the rail (no viewport change at all), at
  320px, and at 640x450 (a standard way to simulate 200% zoom on a 1280px display).
