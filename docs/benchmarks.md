# Benchmarks: what contextlake actually saves

An honest, measured look at what connecting the contextlake MCP server to an AI
coding tool (Claude, Devin, Copilot, Cursor, Windsurf …) does to token usage, and,
just as importantly, where it *doesn't* help.

These numbers come from real measurements against a large private multi-repo estate,
not a synthetic demo. We publish the caveats alongside the wins because a benchmark
without its assumptions is marketing, not data.

## The short version

- **Writing new code in an existing estate is where it matters most.** New code in a
  large codebase is not greenfield invention, it is mostly *grounding*: which service
  to call, its real signature, the pattern to follow, whether a utility already exists,
  where it plugs in. That work is almost entirely retrieval, and it is where hallucinated
  integrations and duplicated code come from. On our estate, grounding one new-code task
  cost **~3,700 tokens with contextlake vs ~16,800 without (~4.6× fewer), before a
  single line is generated**, and with higher confidence, because it matches *concepts*,
  not just keywords.
- **Search of every kind**, semantic / knowledge search, "who calls X", "what breaks
  if I change X", "which repo has X", is the other strong case, where brute-force is
  intractable or hugely wasteful on a large fleet.
- **Assessing impact on and maintaining unfamiliar existing code** benefits for the same
  reason: orientation is retrieval.
- **What it does *not* do (with nuance):** it does not make a *single correct
  generation* shorter, the code you need is the code you need. But across a whole task
  it does cut *total* generation, by reducing failed attempts and reinvented code (see
  [Does it cut generation tokens?](#does-it-cut-generation-tokens)). It also helps little
  on true greenfield work in an empty repo, there is nothing to ground against.
- **On cost:** real but modest on per-token API billing; the larger lever is *rework
  avoided* (a hallucinated integration is a failed build, a re-prompt, and a wasted
  review), which is time and correctness more than a line item.

Treat contextlake as a **scale-and-correctness tool first, a token-cost tool second.**

## Methodology

- **Substrate:** a real, private software estate of **~680 repositories** , 
  ~1.0 million graph nodes, ~1.9 million edges, ~900,000 semantic vectors, and ~21 GB
  of source on disk. (No proprietary names, paths, or symbols are reproduced here.)
- **Token counts:** [`tiktoken`](https://github.com/openai/tiktoken) `cl100k_base`, a
  reasonable cross-model proxy for "tokens". Real source in this estate measured at
  **~4.8 characters per token**.
- **Both sides are measured, not modeled.** The *contextlake* figure is the actual MCP
  tool response an agent ingests (the structured JSON). The *baseline* is what an agent
  gathers without contextlake, `grep` over the same source, plus the file reads that
  follow, measured on the same 21 GB.
- **The baseline is realistic, not worst-case.** A well-scoped agent that greps a single
  known repo is cheaper than one that greps the whole fleet; the figures below sit
  between those extremes.
- Measured on contextlake **v2.33.2**.

## Results (per query)

One fixed cost applies to any session: the **~22 MCP tool schemas load once, ≈ 3,400
tokens**.

| Question | contextlake | Without it (grep + reads) | Ratio / note |
| --- | --- | --- | --- |
| **Where is X defined?** | ~160 tokens, cited | ~6,200 tokens for a common name … down to ~230 for a rare one | **1.4×–~40×**, precise and ranked |
| **Who calls X?** | ~1,400–3,400 tokens (all callers, cited, budgeted) | ~7,000 tokens (100+ raw hits incl. the definition, comments, strings) | **~2–5×**, and *complete* |
| **What breaks if I change X?** | ~9,800 tokens (up to 100 transitive hits, hop-tagged, cross-repo) | grep cannot do transitive reach; first-level only, ~7,000 tokens and incomplete | **a capability, not just a saving** |
| **Semantic / knowledge search** | ~1,100 tokens (8 ranked, cited) | keyword grep across the fleet returns **10–28 million tokens** (100K–200K hits), intractable to read | **orders of magnitude**, with higher recall |
| **Who owns / knows repo X?** | ~300–500 tokens | `git log` (cheap) | modest |
| **Explain / brief a repo** | ~1,300–1,500 tokens | read the README + several files, ~5,000–20,000 tokens | **~4–10×** |

A note on scale: on this estate a single `grep` pass took **3–5 seconds** and returned
tens of thousands of hits (millions of tokens) for a common term, before the agent
reads a single file. contextlake answers the same question from its index in one
bounded, cited call.

## By use case, honestly

### New code development: the most important case

In a real company you rarely write code into a vacuum; you add a feature, endpoint, or
service to an estate that already has hundreds of repositories. Most of that work is
**grounding the new code in existing reality**: which service to call and its real
signature, the established pattern to model on, whether a utility for this already
exists, and where the new code plugs in. That is retrieval, and it is exactly where an
ungrounded model hallucinates an integration or reinvents a helper.

We measured a realistic new-code task, *"add a feature that integrates with an existing
service, following existing conventions"*, as the sequence of grounding questions an
agent asks before it writes anything:

| Grounding step | contextlake | Baseline (grep + reads) |
| --- | --- | --- |
| Find the pattern to follow (semantic) | ~1,100 tok | ~2,700 tok, keyword-only |
| Find how to integrate (semantic) | ~780 tok | ~2,100 tok, keyword-only |
| The API to call (definition) | ~640 tok | (part of the file reads below) |
| Usage examples to model on (callers) | included | included below |
| Check a utility isn't reinvented (semantic) | ~1,050 tok | ~2,000 tok, keyword-only |
| Read candidate service/pattern files |, | ~10,000 tok (≈8 files) |
| **Grounding total** | **~3,700 tokens** | **~16,800 tokens** |

So the grounding phase alone is **~4.6× cheaper**, and finishes with *cited, concept-
matched* context instead of keyword hits the model has to guess from, **before a single
line is generated.** The larger, harder-to-quantify win sits downstream: correct
grounding prevents the hallucinated integration or the duplicate util that turns into a
failed build, a re-prompt, and a wasted review.

What contextlake does **not** shorten is a single *correct* generation, the code you
need is the code you need, and it helps little on true greenfield work in an empty repo,
where there is nothing to ground against.

### Does it cut generation tokens?

Short answer: not the way "measured" implies, but yes at the level of a whole task. A
single correct generation is irreducible. What contextlake reduces is the *number* of
generations and the *amount of new code* generated, ranked by impact:

1. **It cuts the hallucinate → fail → regenerate loop (biggest lever).** The expensive
   thing isn't the first draft; it's the second, third, and fourth. An ungrounded model
   invents an import, calls the wrong service, or guesses a signature; the build fails;
   the agent regenerates. Every retry is generation tokens spent again. Grounding in the
   *real* API/signature/pattern collapses N attempts toward 1, the failure mode that
   dominates new-code work in a large estate.
2. **Reuse instead of reinvent.** When semantic search surfaces an existing
   client/util/validator, the agent emits a *call* (a few tokens) instead of a
   reimplementation (tens–hundreds), and doesn't add a duplicate to the codebase.
3. **Surgical edits instead of full rewrites.** With a precise definition + blast radius,
   the agent emits a small diff rather than regenerating a whole file "to be safe."
4. **Less defensive/exploratory/hedged output.** Precise context yields one confident
   implementation instead of multiple candidate approaches and just-in-case scaffolding.

**The honest boundary:** the irreducible core (typing out the correct code) can't shrink;
if the first generation would already be correct without contextlake, the saving is ~0;
and it can't rescue a model that ignores the context it was handed.

> **This is a mechanism argument, not a measured number.** Everything labeled "measured"
> on this page is retrieval/grounding tokens. Quantifying the generation saving honestly
> means counting *total* output tokens across a real multi-attempt task, with and without
> contextlake, which we have **not** run. Treat the mechanisms above as *why* total
> generation drops, not as a benchmarked figure.

### Search: the other strong case

On a large estate, brute-force search is either intractable (a concept query
keyword-grepped fleet-wide produced *tens of millions* of tokens) or slow and noisy.
contextlake turns it into an O(1), cited, ranked call, and semantic search finds
conceptually-related code that keyword search misses entirely.

### Modifying / maintaining existing code: a moderate win

Largest when the code is unfamiliar. The value is orientation: *where is this, who calls
it, what breaks if I touch it, who owns it*, answered in one cited call instead of many
grep → read → re-grep round-trips.

## Translating to ACU / dollars / subscription

Different tools bill differently, so the honest answer differs per platform.

- **Claude via MCP (per-token API).** This is the only place token deltas map *directly*
  to money. But a per-*query* ratio is not a per-*session* saving: most agent tokens are
  reasoning, generation, and the conversation growing turn over turn, not retrieval.
  Expect roughly **10–40% fewer *input* tokens on retrieval-heavy sessions, and ≈0 on
  generation-heavy ones.** (Separately, *output* tokens can drop too, via fewer failed
  regenerations and less reinvented code, but that is a mechanism, not a measured figure;
  see [Does it cut generation tokens?](#does-it-cut-generation-tokens).) Meaningful across
  a team and a large estate; not a headline cut on any single session.

- **Devin (ACU).** We did **not** measure this, and anyone quoting you an ACU number is
  guessing. An ACU is compute-time, not tokens; the plausible lever is *exploration
  steps and rework avoided*, which is highly task-dependent. Expect a meaningful
  reduction on exploration-heavy, unfamiliar-fleet tasks and ≈0 on tightly-scoped
  generation tasks, a hypothesis to A/B test, not a measured claim.

- **VS Code + Copilot (flat subscription).** There is no per-token bill to save. The
  value is **accuracy and speed**: fewer wrong suggestions from missing cross-repo
  context, better-targeted file reads, and staying inside the context window. It shows
  up as developer time and correctness, not a line item.

## Caveats worth reading

- These are **per-query** figures. Whole-task savings are diluted by all the
  non-retrieval tokens around them, don't multiply a 40× onto your whole bill.
- The **baseline depends on how the agent searches.** A smart, well-scoped agent spends
  less than the fleet-grep baseline; a naive one spends more.
- contextlake adds a **fixed ~3,400-token schema cost** per session and can be
  net-negative if an agent calls it for questions it doesn't help with.
- **Semantic recall is good, not perfect**, the built-in CPU embedder is fast, not
  frontier-grade. Results are cited and advisory; verify against the source.
- Measured on **one large estate**; your mileage varies with fleet size and the mix of
  questions your team actually asks.

## Reproduce it

The measurements above are straightforward to reproduce on your own repositories:

1. Index your repos (`contextlake bootstrap` or `contextlake index --workspace …`).
2. Pick a handful of representative questions your team actually asks.
3. For each, measure **both** the contextlake MCP response (the JSON the agent
   receives) and the baseline your agent would otherwise gather (`grep` output + the
   files it would read), and tokenize both with the same tokenizer
   (`tiktoken` `cl100k_base` here).

The point is not a single magic number, it is to see, on *your* codebase and *your*
question mix, where the retrieval cost actually lives.
