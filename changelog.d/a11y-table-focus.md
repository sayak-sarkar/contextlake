### Fixed
- **Dashboard: the Fleet table's rows have a visible keyboard-focus indicator again
  (WCAG 2.4.7 Focus Visible, 1.4.11 Non-Text Contrast).** The row's `:hover` and
  `:focus-visible` states shared one rule that also set `outline: none`, so a keyboard
  user tabbing through the Table layout landed on a row that looked identical to its
  neighbors -- measured at the time as roughly a 1.05:1 background-colour shift, far
  under the 3:1 minimum a UI-component focus indicator needs, and with no border or
  shadow standing in for the removed outline. Every other interactive surface in the
  dashboard (`.cl-repocard`, `.cl-reporow`, and the global `:focus-visible` rule these
  rows now fall back to) already keeps the ordinary 2px outline; this was the one
  selector overriding it to nothing. Removing the override lets the global outline
  apply, and its colour clears 3:1 against the row background in both themes (measured
  via `getComputedStyle` on a really-tabbed-to row, not computed from source: ~4.6:1
  light, ~4.9:1 dark).
