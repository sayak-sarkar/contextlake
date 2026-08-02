# Ownership and SMEs

`contextlake kb owners <repo>` (or `--path SUBDIR` for a sub-tree) answers **"who owns this, who do I ask?"**
straight from git history, no config, no index required. It ranks contributors by a **recency-weighted**
blend of commit volume and lines changed, so someone active in that area lately outranks a long-departed
prolific author:

```bash
contextlake kb owners acme/payments-api                 # top contributors for the whole repo
contextlake kb owners acme/payments-api --path src/auth  # ...scoped to the auth module
contextlake kb owners acme/payments-api --json           # machine-readable, for scripts/CI
```

<p align="center">
  <img src="https://raw.githubusercontent.com/sayak-sarkar/contextlake/main/docs/img/cli/cli-owners.png" alt="contextlake kb owners acme/catalog-api output: a recency-weighted SME ranking from git history: Ada Lovelace (2 commits, 29 lines, 94%) above Grace Hopper (1 commit, 6%)." width="820">
</p>

The same ranking is available to agents over MCP as **`who_knows(repo, path?, limit?)`**.

## See also

- [Serve it to your editor](serve.md)
- [The dashboard](dashboard.md)
- [Index the code graph](index-code-graph.md)
