### Fixed

- **`kb index`'s bundling advice now names the directory you actually gave it.** The remedy it
  prints when a directory holds git repositories was the hardcoded string `contextlake kb index
  --workspace .`, but the directory being indexed comes from the positional path or `--source`
  and only falls back to `.` when neither was given. So `kb index /srv/fleet` was told to run
  `--workspace .` -- the shell's current directory, not the one just named. Followed verbatim it
  indexes the wrong tree.

  In a real run it cost coverage a subtler way than that. The operator saw that `.` was wrong,
  reasonably inferred the fleet lived one level down, and ran `--workspace ./repositories`; the
  repository sitting above that subdirectory was then never indexed under its own identity at
  all, only inside the bundle. Advice that cannot be followed literally is not a cosmetic defect,
  because the reader has to guess, and a plausible guess was wrong.

  The message now echoes the path as it was typed -- shell-quoted only when the path would not
  survive a shell -- so it stays `.` for a bare `kb index` run, where the short form is both
  correct and the command the reader will recognise as their own.
