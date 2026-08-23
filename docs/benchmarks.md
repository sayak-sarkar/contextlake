# What contextlake saves, and how to measure it

What connecting the contextlake MCP server to an AI coding tool (Claude, Devin, Copilot, Cursor,
Windsurf …) does to token usage, where it helps most, and where it does not help at all.

> [!IMPORTANT]
> **This page publishes no benchmark figures, on purpose.** It used to. Those numbers were taken on
> one private estate and no script, dataset, or result file was ever committed with them, so nobody,
> including us, could re-run them. A figure you cannot reproduce is a claim wearing a number's
> clothes, so they have been removed rather than restated. What is left is the mechanism, which you
> can check by reading the code, and a procedure for measuring the effect on your own repositories,
> which is the only place a number should decide anything for you.
>
> There is now exactly one set of figures this project does publish, and it arrives with the script
> that produced it: `benchmarks/head-to-head/` compares graph coverage against another local
> code-graph tool over four pinned public repositories. It answers a **different** question from
> this page. It measures how much of a tree each tool represents, not what either saves you in
> tokens, and contextlake leads on one of the four.

## The short version

- **Writing new code in an existing estate is where it matters most.** New code in a large codebase
  is not greenfield invention, it is mostly *grounding*: which service to call, its real signature,
  the pattern to follow, whether a utility already exists, where it plugs in. That work is almost
  entirely retrieval, and it is where hallucinated integrations and duplicated code come from.
  contextlake answers those questions from an index, cited, and matches *concepts* rather than just
  keywords.
- **Search of every kind**, semantic / knowledge search, "who calls X", "what breaks if I change X",
  "which repo has X", is the other strong case, where brute-force is intractable or hugely wasteful
  on a large fleet.
- **Assessing impact on and maintaining unfamiliar existing code** benefits for the same reason:
  orientation is retrieval.
- **What it does *not* do (with nuance):** it does not make a *single correct generation* shorter,
  the code you need is the code you need. But across a whole task it does cut *total* generation, by
  reducing failed attempts and reinvented code (see
  [Does it cut generation tokens?](#does-it-cut-generation-tokens)). It also helps little on true
  greenfield work in an empty repo, there is nothing to ground against.
- **On cost:** real but modest on per-token API billing; the larger lever is *rework avoided* (a
  hallucinated integration is a failed build, a re-prompt, and a wasted review), which is time and
  correctness more than a line item.

Treat contextlake as a **scale-and-correctness tool first, a token-cost tool second.**

## Where the difference comes from

Per question, what the agent ingests with contextlake against what it gathers without it. This is a
description of the two mechanisms, not a measurement of either:

| Question | contextlake | Without it (grep + reads) |
| --- | --- | --- |
| **Where is X defined?** | One ranked, cited definition per candidate | Every textual occurrence of the name, then the file reads needed to tell a definition from a mention |
| **Who calls X?** | The resolved call edges, cited and budgeted | Raw hits including the definition, comments, and strings, with no way to tell a call from a coincidence |
| **What breaks if I change X?** | Transitive dependents, hop-tagged, across repos | First-level textual hits only |
| **Semantic / knowledge search** | Ranked, cited hits for a *concept* | Keyword hits for whichever keyword you guessed |
| **Who owns / knows repo X?** | Ranked from git history in one call | `git log`, which is cheap and already local |
| **Explain / brief a repo** | One brief assembled from the graph and the wiki | The README plus however many files it takes |

Rows 3 and 4 are capability differences rather than savings: **transitive reach** and **concept
matching** are not things a grep-and-read loop does more expensively, they are things it does not do
at all, at any budget. The definition and caller rows are where a token saving is plausible, and its
size depends entirely on how common the name is and how disciplined the agent's search would
otherwise have been. The ownership row is where contextlake saves least, because `git log` was
already cheap.

The other structural difference is that brute-force search costs *time* as well as tokens on a large
fleet: a fleet-wide grep for a common term returns more hits than an agent can read, before it opens
a single file. contextlake answers the same question from its index in one bounded, cited call.

## The one fixed cost

Any session pays for the tool schemas once, whether or not it calls a tool: **23 of them on a
graph-only store** (22 graph tools plus the `ask` router) and **25 once embeddings exist**, all
listed in [Serve](serving-over-mcp.md). That cost is real, and it can leave you net-negative if the agent
reaches for the server on questions it does not help with. There is no published token figure for it
here for the reason at the top of the page; the recipe in
[Measure it on your own repositories](#measure-it-on-your-own-repositories) counts it in the same
pass as everything else.

## By use case, honestly

### New code development: the most important case

In a real company you rarely write code into a vacuum; you add a feature, endpoint, or service to an
estate that already has hundreds of repositories. Most of that work is **grounding the new code in
existing reality**: which service to call and its real signature, the established pattern to model
on, whether a utility for this already exists, and where the new code plugs in. That is retrieval,
and it is exactly where an ungrounded model hallucinates an integration or reinvents a helper.

A realistic new-code task, *"add a feature that integrates with an existing service, following
existing conventions"*, decomposes into a sequence of grounding questions the agent asks before it
writes anything:

1. Find the pattern to follow (semantic search, no exact name to grep for).
2. Find how to integrate with the existing service (semantic search again).
3. Look up the API to call (definition lookup).
4. Find usage examples to model on (callers).
5. Check a utility for this is not already there (semantic search).

With contextlake, each is one cited call. Without it, steps 1, 2 and 5 have no keyword to search for
and degrade into reading candidate files, which is where the bulk of the baseline's tokens go. The
grounding phase finishes with *cited, concept-matched* context instead of keyword hits the model has
to guess from, **before a single line is generated.** The larger, harder-to-quantify win sits
downstream: correct grounding prevents the hallucinated integration or the duplicate util that turns
into a failed build, a re-prompt, and a wasted review.

What contextlake does **not** shorten is a single *correct* generation, the code you need is the code
you need, and it helps little on true greenfield work in an empty repo, where there is nothing to
ground against.

### Does it cut generation tokens?

Short answer: not the way "measured" implies, but yes at the level of a whole task. A single correct
generation is irreducible. What contextlake reduces is the *number* of generations and the *amount of
new code* generated, ranked by impact:

1. **It cuts the hallucinate → fail → regenerate loop (biggest lever).** The expensive thing isn't
   the first draft; it's the second, third, and fourth. An ungrounded model invents an import, calls
   the wrong service, or guesses a signature; the build fails; the agent regenerates. Every retry is
   generation tokens spent again. Grounding in the *real* API/signature/pattern collapses N attempts
   toward 1, the failure mode that dominates new-code work in a large estate.
2. **Reuse instead of reinvent.** When semantic search surfaces an existing client/util/validator,
   the agent emits a *call* (a few tokens) instead of a reimplementation (tens to hundreds), and
   doesn't add a duplicate to the codebase.
3. **Surgical edits instead of full rewrites.** With a precise definition + blast radius, the agent
   emits a small diff rather than regenerating a whole file "to be safe."
4. **Less defensive/exploratory/hedged output.** Precise context yields one confident implementation
   instead of multiple candidate approaches and just-in-case scaffolding.

**The honest boundary:** the irreducible core (typing out the correct code) can't shrink; if the
first generation would already be correct without contextlake, the saving is ~0; and it can't rescue
a model that ignores the context it was handed.

> [!WARNING]
> **This is a mechanism argument, not a measured number.** Quantifying the generation saving honestly
> means counting *total* output tokens across a real multi-attempt task, with and without
> contextlake, which we have **not** run. Treat the mechanisms above as *why* total generation drops,
> not as a benchmarked figure.

### Search: the other strong case

On a large estate, brute-force search is either intractable (a concept query has no single keyword to
grep for, and the keyword you pick returns more than an agent can read) or slow and noisy.
contextlake turns it into a bounded, cited, ranked call, and semantic search finds conceptually
related code that keyword search misses entirely.

### Modifying / maintaining existing code: a moderate win

Largest when the code is unfamiliar. The value is orientation: *where is this, who calls it, what
breaks if I touch it, who owns it*, answered in one cited call instead of many grep → read → re-grep
round-trips.

## Translating to ACU / dollars / subscription

Different tools bill differently, so the honest answer differs per platform.

- **Claude via MCP (per-token API).** This is the only place token deltas map *directly* to money.
  But a per-*query* ratio is not a per-*session* saving: most agent tokens are reasoning, generation,
  and the conversation growing turn over turn, not retrieval. A retrieval-heavy session is where any
  input-token saving shows up at all, and a generation-heavy one is where it disappears. (Separately,
  *output* tokens can drop too, via fewer failed regenerations and less reinvented code, but that is
  a mechanism, not a measured figure; see
  [Does it cut generation tokens?](#does-it-cut-generation-tokens).)

- **Devin (ACU).** We did **not** measure this, and anyone quoting you an ACU number is guessing. An
  ACU is compute-time, not tokens; the plausible lever is *exploration steps and rework avoided*,
  which is highly task-dependent. Expect a meaningful reduction on exploration-heavy,
  unfamiliar-fleet tasks and ≈0 on tightly-scoped generation tasks, a hypothesis to A/B test, not a
  measured claim.

- **VS Code + Copilot (flat subscription).** There is no per-token bill to save. The value is
  **accuracy and speed**: fewer wrong suggestions from missing cross-repo context, better-targeted
  file reads, and staying inside the context window. It shows up as developer time and correctness,
  not a line item.

## Caveats worth reading

- Whatever you measure will be **per-query**. Whole-task savings are diluted by all the non-retrieval
  tokens around them, so don't multiply a per-query ratio onto your whole bill.
- The **baseline depends on how the agent searches.** A smart, well-scoped agent that greps one known
  repository spends far less than one that greps the whole fleet. Both are realistic; they are not
  the same baseline, and a ratio means nothing without saying which one you used.
- contextlake adds a **fixed schema cost per session** and can be net-negative if an agent calls it
  for questions it doesn't help with.
- **Semantic recall is good, not perfect**, the built-in CPU embedder is fast, not frontier-grade.
  Results are cited and advisory; verify against the source.
- Fleet size and the mix of questions your team actually asks dominate everything here. A result from
  someone else's estate does not transfer to yours, which is the other half of why this page no
  longer prints one.

## Measure it on your own repositories

The comparison is straightforward to run, and yours is the one worth having:

1. Index your repos (`contextlake bootstrap` or `contextlake kb index --workspace …`).
2. Pick a handful of representative questions your team actually asks, and write them down before
   you look at any output. Post-hoc question selection is how benchmarks flatter themselves.
3. For each question, capture **both** sides: the contextlake MCP response (the JSON the agent
   receives, which you can get from `contextlake kb query --json`, `kb impact --json`, and the other
   `--json` commands) and the baseline your agent would otherwise gather (the `grep` output plus the
   files it would then read).
4. Tokenize both with the **same** tokenizer, the one your model actually uses, and record the
   tokenizer and the contextlake version alongside the numbers. Both change; a figure without them
   ages into fiction, which is what happened to the numbers this page used to carry.
5. Add the one-off schema cost once per session, not once per query.

The point is not a single magic number, it is to see, on *your* codebase and *your* question mix,
where the retrieval cost actually lives.
