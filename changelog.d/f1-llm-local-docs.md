### Fixed
- **Docs: the working `llm-local` install command is now unmissable wherever `--llm builtin` is
  offered.** `pip install "contextlake[kb-full,llm-local]"` fails on a machine with no C/C++
  compiler, building `llama-cpp-python` from source: upstream ships no PyPI wheels, so a plain
  install always compiles. The working form already existed at `docs/install.md` (`contextlake
  doctor --fix llm-local`, which attaches the prebuilt CPU wheel index), and the runtime failure
  in the built-in LLM client already named it too, but neither was reachable from the places a
  user actually goes to turn on the wiki's local model: `docs/keep-fresh.md`'s `bootstrap --llm
  builtin` example, `docs/generate-wiki.md`'s `kb wiki --llm builtin` example, and
  `docs/dashboard.md`'s copy-paste Wiki-tab command all showed or named `builtin` with no pointer
  to the extra step it needs, and the command-reference tables in `README.md` and
  `docs/cli-reference.md` listed `--llm builtin` alongside `ollama`/`openai`/`anthropic`/`cli` with
  no hint that one of those five needs anything extra at all. Each now names `contextlake doctor
  --fix llm-local` (or links to the existing `docs/install.md` section that does) right next to the
  `--llm builtin` example it sits beside, and calls out that `--llm ollama` needs no compiler at
  all as the no-install alternative. `docs/install.md`, `docs/model-providers.md`,
  `docs/troubleshooting.md` and `QUICKSTART.md`'s bootstrap walkthrough already covered this
  correctly and needed no change beyond one added clause in QUICKSTART.md naming ollama's
  no-compiler property explicitly.
