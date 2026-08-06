### Fixed
- **Dashboard: repo health is no longer colour-only, and its dot now clears the
  non-text contrast minimum (WCAG 1.4.1 Use of Color, 1.4.11 Non-Text Contrast).** The
  Fleet page's Cards, List and Table layouts all rendered a repo's health as a small
  solid dot with no text -- the only other information was a native `title` tooltip
  (mouse-hover only, not available to touch or keyboard users). Every health chip now
  also carries a visible short label ("Fresh"/"Stale"), matching the pattern the repo
  page already used elsewhere ("HEAD moved", "no checkout") and the one the confidence
  chips use correctly (fill + border-style + glyph + a visible label, never colour
  alone). The dot's own fill colour was also measured at 2.40:1 against its row
  background -- under the 3:1 a graphical state indicator needs even for a sighted
  user who can perceive colour -- and is now a darker, more saturated teal that clears
  3:1 against both themes' backgrounds (measured rendered: light 3.5-3.8:1, dark
  3.4-3.5:1), not just estimated from the source values.
- **Dashboard: the confidence trust-bar's clickable segments now meet the WCAG 2.5.8
  minimum pointer-target size (24x24 CSS px), including on the narrow/zoomed viewports
  where the only other way to reach the same filter is hidden.** The segments were
  14px tall subdivisions of a continuous track with no gap between them -- under the
  minimum, with no rescuing "equivalent control" or "enough surrounding space"
  exception available once the header's confidence-filter buttons are hidden below
  768px (the same breakpoint zooming to 200% on a typical laptop display reaches). The
  track is now tall enough that each segment's real hit box -- not just its painted
  colour -- clears 24px, verified by checking that `elementFromPoint` at the very top
  and bottom of the reported box still resolves to the segment (a naive `min-height` on
  a clipped ancestor can report the right number while the actual hit area stays
  small); the bar's visual height is otherwise close to unchanged.
