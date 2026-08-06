### Fixed
- **Dashboard: keyboard focus no longer jumps into main content on the very first page
  load (WCAG 2.4.3 Focus Order).** The router moves focus to `#app` on a genuine route
  change so a keyboard user navigating between lenses (Fleet, Architecture, Chat, ...)
  lands where the new content starts -- a deliberate, correctly-motivated fix for a real
  problem. But the guard that decides "is this a route change" compared the new route
  against a `null` starting value, so the very *first* render (page load, before the
  user has tabbed anywhere) always satisfied it too, and focus jumped to `#app` before
  the skip link could ever be used -- inverting the effective tab order so the header
  and primary navigation, which come first visually, came last in the sequence a
  forward-tabbing user actually experiences. Reloading the dashboard now leaves focus at
  the top of the document (`document.activeElement` is `<body>`, matching a fresh page
  load), while an actual navigation still moves focus into the new panel exactly as
  before, and an in-view re-render (a filter toggle, a trust-bar click -- same route, no
  navigation) still does not steal focus. The skip link is no longer dead on arrival.
