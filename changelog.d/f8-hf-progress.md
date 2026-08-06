### Fixed
- **HuggingFace Hub download progress bars no longer leak into `kb connect`/`kb embed`.**
  `hush_hf_hub()` was already called before every local-model download, but its env vars and
  logger-level settings gate HF Hub's own logging and deprecation warnings -- never the
  separate `tqdm` progress-bar switch, so three bars still rendered per fetch, two of them
  showing no file name or percentage (only a byte count stuck at `0.00B`). Progress bars are
  now hushed there too, unless `--verbose` was passed -- a verbose run still sees them, e.g.
  to confirm a large model is actually moving.
