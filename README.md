# Verifiable Code Intelligence Plugin

This plugin combines semantic discovery and structural analysis behind three
stable primitives: **FIND**, **UNDERSTAND**, and **PROVE**. Its differentiator
is not an unbounded accuracy claim. It is an evidence contract: coherent
indexes for one exact checkout, generation-bound references, explicit
contradiction and coverage checks, and deterministic proof verdicts that can
be exported and independently verified.

**INTEGRATED READINESS: READY**

The current BOM declares the pinned components ready for coherent dual
indexing. The committed `promotion-candidate` record is repository-controlled
review input; on every trusted `main` revision, post-merge CI must install the
exact pins and generate a fresh `ready-validation` record before that revision
is accepted as runtime-validated. `/index-repo` is the integrated semantic and
structural indexing workflow.

## Quick Start

```bash
# 1. Clone the plugin
git clone https://github.com/redacted-org/codebase-search-plugin.git
cd codebase-search-plugin

# 2. Run the install script (downloads both MCP servers)
bash install.sh            # Linux/Mac
# pwsh install.ps1         # Windows (PowerShell)

# 3. Set your embedding provider
export EMBEDDING_PROVIDER="voyage"          # Voyage 4 Large (cloud)
export VOYAGE_API_KEY="your-key"
# OR, keep both indexes on-device
unset VOYAGE_API_KEY
export EMBEDDING_PROVIDER="jina"            # local code-search embeddings
export CODE_GRAPH_SKIP_EMBEDDINGS=1         # disable graph cloud embeddings

# 4. Register this installed clone and install the namespaced plugin
claude plugin marketplace add "$PWD"
claude plugin install codebase-search@redacted-code-intelligence

# 5. Build and verify both indexes
/index-repo /path/to/your/repo

# 6. Ask through the stable FIND / UNDERSTAND / PROVE facade
/code-intel Find the request authentication entry points
```

This runs both semantic and structural indexing, suppresses graph report
writes, and verifies that both engines indexed the same unchanged checkout.
`EMBEDDING_PROVIDER` controls code-search only. If code-graph inherits
`VOYAGE_API_KEY`, it independently sends selected node and query text to
Voyage unless `CODE_GRAPH_SKIP_EMBEDDINGS=1`.

The install script:
- Creates a Python venv and installs the exact code-search source declared by
  the BOM: either a pinned Git commit or an attested GitHub Release wheel
- Downloads the pre-built code-graph binary for your platform from GitHub releases
- Reads exact tested versions from `component-bom.json` instead of selecting a moving latest release
- Starts both installed stdio MCPs and rejects missing or schema-drifted tools before reporting success
- No manual cloning, building, or path configuration needed

> **Current compatibility contract:** the BOM declares complete v1
> `index_identity` outputs, semantic and graph readiness, and graph
> `skip_report` support. The committed promotion candidate is supporting
> review evidence, not a trusted run-specific attestation. Trusted post-merge
> CI must reproduce readiness from the exact pins. See
> `compatibility/README.md`.

## How It Works

The ready integrated design combines two different search technologies.

### Semantic Search (code-search)

**What it does:** Finds code by *meaning*, not just keywords. When you search "authentication middleware," it returns functions related to auth even if they're named `verify_jwt_token` or `check_session`.

**How:** Your code is split into chunks (functions, classes, modules) using tree-sitter AST parsing. Each chunk is converted into a numeric vector (embedding) that captures its meaning. Search queries are also converted to vectors, and the closest vectors are returned. A keyword index (BM25) runs in parallel and results are fused together.

**When to use:**
- "How does X work?" — conceptual understanding
- "Find the rate limiting implementation" — discovery by meaning
- "Where is the config for Y?" — locating code you know exists but can't grep for

### Structural Graph (code-graph)

**What it does:** Maps the *structure* of your code — which functions call which, what imports what, how modules connect. Answers questions about relationships and impact, not content.

**How:** Tree-sitter parses your code into an AST, extracts symbols (functions,
classes, routes, imports), and builds a knowledge graph stored in SQLite. That
structural extraction is local. When `VOYAGE_API_KEY` is present, code-graph
also sends selected node text to Voyage during indexing and sends query text
for embedding-backed graph searches. Set `CODE_GRAPH_SKIP_EMBEDDINGS=1` before
launching the MCP to disable graph embedding generation. Queries use a
Cypher-like language to traverse call chains, find dead code, and calculate
blast radius.

**When to use:**
- "What calls processOrder?" — tracing call chains
- "Blast radius of changing UserService?" — impact analysis
- "Find dead code" — functions with zero callers
- "Show all API endpoints" — structural inventory

### How They Work Together

`/code-intel` presents three stable public primitives while preserving the
same automatic backend routing and cross-engine coherence checks:

| Public primitive | Routed capability | Example |
|------------------|-------------------|---------|
| FIND | Semantic or lexical code-search | "Find the authentication middleware" |
| UNDERSTAND | Structural code-graph, optionally chained from FIND | "What calls processOrder?" |
| PROVE | Coherent evidence from both engines plus deterministic contradiction and coverage evaluation | "Prove every request path passes through authorization" |

You do not need to select a backend. `/code-explore` remains available as the
compact natural-language discovery and relationship workflow; it uses the
same engines and preserves canonical evidence when the installed components
expose it.

### Skills

| Skill | Purpose |
|-------|---------|
| **`/index-repo`** | Index both engines and verify their readiness and checkout identities |
| **`/code-intel`** | Stable FIND / UNDERSTAND / PROVE facade with coherence, contradiction, and coverage rules |
| **`/code-explore`** | Ask natural language questions — auto-routes to the right search tool |

## Offline vs Online Embedding Models

"Embedding" means converting code into numeric vectors for similarity search. The plugin supports both **online** (cloud API) and **offline** (local) models.

### Online: Voyage AI

For code-search, source chunks and search queries are sent to Voyage AI's API
over HTTPS and returned as vectors. The pinned code-search release exposes
three distinct Voyage selectors:

| Provider | Model | Current role |
|----------|-------|--------------|
| **`voyage`** | **`voyage-4-large`** | Default for code when `VOYAGE_API_KEY` is present |
| `voyage-context` | `voyage-context-3` | Contextualized provider; auto-selected for documentation mode |
| `voyage-code-3` | `voyage-code-3` | Separate legacy, non-default provider retained for older and TypeScript-specific workflows |

`voyage` is not an alias for `voyage-code-3`. All three providers send code
and query text off-device.

- Requires internet connection and a Voyage API key
- ~$0.06 per 1M tokens (~$2-5 to index a large monorepo)
- Indexing speed limited by API rate limits (~5-10 min per 3K chunks)

Code-graph does not use `EMBEDDING_PROVIDER`, but it does independently use
`VOYAGE_API_KEY` for optional node embeddings during indexing and for
embedding-backed graph queries. Its default Voyage model is separate from
code-search's provider mapping. `CODE_GRAPH_SKIP_EMBEDDINGS=1` prevents graph
node-embedding generation even if the key is present; remove the key from the
MCP environment to prevent all graph Voyage calls, including query embedding.

**Use cloud embeddings only when:** Your code and query text are permitted to
leave the machine.

### Offline: Jina Code Embeddings (`jina`)

A 494M parameter model runs code-search embeddings entirely on your CPU. The
model weights are downloaded once (~1GB) from HuggingFace on first use, then
code-search embedding and query operations are local.

- Historical component measurements below put Jina near or above the older
  `voyage-code-3` run on the measured subprojects; they do not compare Jina
  with the current `voyage-4-large` mapping
- No API key, no internet (after first download), no cost
- Indexing is CPU-bound (~50 min per 3K chunks without GPU)
- Query latency ~5s vs ~1s for Voyage

**Use when:** Code cannot leave the machine, you don't want API keys, or you're
evaluating the plugin before committing to a paid provider. For a fully
on-device plugin run, remove `VOYAGE_API_KEY` from code-graph's environment;
setting `CODE_GRAPH_SKIP_EMBEDDINGS=1` additionally prevents node-embedding
generation if a key is later inherited.

### Switching Between Providers

The provider is selected at **runtime** via environment variable — not at install time. You can switch freely:

```bash
# Switch to local
export EMBEDDING_PROVIDER="jina"

# Switch code-search to the current cloud default
export EMBEDDING_PROVIDER="voyage"
export VOYAGE_API_KEY="pa-..."
```

**Important:** Switching providers requires re-indexing because indexes are
provider-specific and are not interchangeable.

## Prerequisites

- **Python 3.12+** (for code-search)
- **GitHub CLI (`gh`) with authenticated read access** to both private
  component repositories
- **Linux/Mac**: `tar` (for extracting the verified code-graph archive)
- **Windows**: PowerShell 5.1+ (built-in) — use `install.ps1` instead of `install.sh`

The `install.sh` script handles everything else — no need to manually clone or build anything.

### Manual install (alternative)

The production BOM pins code-search release
[`v0.3.0`](https://github.com/redacted-org/code-search/releases/tag/v0.3.0)
with `install.kind: github-release`. Its descriptor fixes the source commit,
wheel name and SHA-256, `SHA256SUMS` manifest name and SHA-256, JSONL
attestation bundle name and SHA-256, signer workflow, and `refs/heads/main`;
use those values directly rather than selecting a moving release.

For a manual install, follow the same order as the installers:

1. Resolve the exact Git tag through the Git refs API, peel annotated tags,
   and require that the tag resolves to the pinned source commit.
2. Use authenticated `gh release download` to fetch exactly the wheel,
   checksum manifest, and attestation bundle named by the BOM and tag.
3. Verify all three files against their separate BOM SHA-256 values, then
   require exactly one manifest entry matching the wheel name and digest.
4. Run `gh attestation verify` with the offline `--bundle`, pinned repository,
   `--signer-workflow`, `--source-digest`, `--source-ref refs/heads/main`, and
   `--deny-self-hosted-runners`.
5. Only after those checks pass, pip-install the local wheel with
   `--force-reinstall` and run `scripts/verify_code_search_wheel.py` to verify
   its version, filename, checksum, and PEP 610 installation provenance.

For code-graph, use release
[`v0.8.0-redacted.2`](https://github.com/redacted-org/code-graph/releases/tag/v0.8.0-redacted.2).
Resolve its tag to the BOM's pinned source commit; download exactly the
platform archive and `checksums.txt`; verify both BOM digests and the exact
archive manifest entry; verify the operator-fetched, vendored JSONL bundle at
the path and SHA-256 pinned by the BOM; then run secret-free
`gh attestation verify --bundle` with the pinned repository, release workflow,
source digest, `refs/heads/main`, and GitHub-hosted-runner policy. Extract only
after every check passes. Configure the two verified MCP server paths manually
in `.mcp.json`.

Manual installs must match `component-bom.json`. Run the same fail-closed
contract check used by the installers before enabling the plugin:

```bash
python3 scripts/validate_installed.py \
  --server code-search=/path/to/code-search-mcp \
  --server code-graph=/path/to/codebase-memory-mcp
```

## Routing and Evidence Evaluation

`bench/e2e/` contains a deterministic standard-library harness for recorded
host-model traces. It scores routing accuracy, evidence precision/recall,
unsupported claims, tool calls, latency, and stale/mismatched-index handling.
The bundled runs validate the fixture and CI gate only; they are explicitly
not live performance results or comparative grades. See
`bench/e2e/README.md` for the JSONL contract and live-run workflow.

`bench/e2e/pilot/run.py` is the bounded operator runner used for a real
four-arm smoke: native tools, code-search, code-graph, and the composed
workflow. Its v2 preregistration fixes eight cases, two fresh-session
repetitions, scoring rules, a directed-trace efficiency contract, one false
candidate assertion, the model alias, and the activation bar before execution.
Each run preserves raw model transcripts by repetition, scored projections, the
exact component BOM, and SHA-256 bindings for every artifact. This small fixed
fixture improves stability and falsification evidence; it is not a statistical
superiority or broad accuracy claim.

`preregistration-v3.json` separately binds one composed-only confirmation after
the primary run exposed routing, endpoint-evidence, and exact-claim failures.
Pass it with `--preregistration`. The confirmation keeps the original cases,
model, repetitions, scoring, thresholds, and component binaries; it cannot
rewrite the primary result or establish a broad ranking.

The content-addressed five-arm localization instrument lives under
`bench/compare/`; see `bench/compare/README.md` for frozen controls, fixture
falsifiers, public-pin requirements, privacy boundaries, and the current
fail-closed live-preflight status.

### Portable proof packets

After `proof_evaluator.py` accepts a proof bundle, export a deterministic
packet containing the canonical bundle, evaluator result, concise Markdown
report, and content-addressed manifest:

```bash
python3 scripts/export_proof.py export proof-bundle.json \
  --output-dir proof-packet
python3 scripts/export_proof.py verify proof-packet
```

Verification recomputes every artifact digest and reruns the deterministic
evaluator. A changed bundle, result, report, manifest, or evaluator outcome is
rejected. The validation workflow exports and verifies the committed proof
fixture before publishing the packet as a retained CI artifact.

## Trusted component validation

The ordinary pull-request workflow has a stable, fail-closed `merge-gate`
whose only dependency is the deterministic `validate` job. It does not read a
component token. Trusted installation is isolated in
`.github/workflows/trusted-component-promotion.yml`; its
`validate-installed-components` job installs both private repositories from
the exact descriptor path passed with `--component-bom` and validates their
real `tools/list` responses. It runs only from a trusted `main` push or a
manual default-branch dispatch, never on `pull_request`.
`CODE_INTEL_COMPONENT_TOKEN` is a required post-merge validation secret:
configure a fine-grained token with read access to
`redacted-org/code-search` and `redacted-org/code-graph`.
The validator exposes it only to authenticated GitHub fetch/tag-resolution
commands and removes it before package builds or MCP processes start.

For the release-wheel path, repository `Contents: read` is sufficient to
resolve and peel the tag and download its private assets. The wheel is treated
as an attested build artifact downloaded from that pinned release; the checks
do not cryptographically prove its placement there. Its separately
checksum-pinned offline attestation bundle is passed directly to
`gh attestation verify`; no online Attestations API lookup is used, so the
search verification does not need `Attestations: read`. Code-graph uses an
operator-fetched canonical bundle vendored under `compatibility/attestations/`;
the graph descriptor pins its repository-relative path and SHA-256, and static
validation rejects a missing, modified, or release-mismatched bundle. The
bundle covers all five immutable platform archives, so runtime verification is
also offline and does not need `Attestations: read`. Both policies bind the
build to the pinned source commit, release workflow, `refs/heads/main`, and
GitHub-hosted runners.

There is currently no repository secret fallback. If the secret is absent,
the trusted job intentionally fails; do not skip or weaken this validation.
Because the current BOM is `status: ready`, the same job invokes the readiness
smoke generator against the just-installed MCP executables on every trusted
`main` push or manual default-branch run. The fresh `ready-validation` file
stays under the isolated runner directory and must pass the full version,
readiness, identity-shape, generation, binding, and unchanged-checkout
validator. The committed `promotion-candidate` record cannot substitute for
that run-specific attestation. The uploaded trusted artifact includes freshly
captured schema contracts and readiness evidence, each bound to the canonical
SHA-256 of the complete install descriptor.

Rollback is descriptor-atomic: revert the reviewed component-promotion commit
that changed the BOM, snapshots, and readiness record together, then rerun the
deterministic validation gate and trusted workflow. Never roll back only a tag,
digest, manifest, or evidence file.

The interactive installers also preserve the previously installed `.venv` and
`bin` until the replacement components pass their live MCP schema check. Graph
artifacts and launchers are built in an isolated `.install-staging` directory;
if any install, provenance, extraction, or validation step fails, the rollback
handler restores the prior installation instead of leaving a partial upgrade.

## Environment Variables

These control embedding behavior across the two MCPs. `EMBEDDING_PROVIDER`
applies only to code-search. Set variables in your shell profile (`.bashrc`,
`.zshrc`) or before launching Claude Code. They're read at runtime, so you can
switch providers without reinstalling.

**For Jina (local, free):**
```bash
export EMBEDDING_PROVIDER="jina"
```

**For Voyage AI (cloud code-search default):**
```bash
export EMBEDDING_PROVIDER="voyage"
export VOYAGE_API_KEY="pa-..."  # Get a key at https://dash.voyageai.com
```

**If neither is set**, code-search auto-selects `voyage` for code when
`VOYAGE_API_KEY` exists and `local` otherwise. Documentation indexing selects
`voyage-context` when the Voyage key exists.

**All variables:**

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `EMBEDDING_PROVIDER` | No | Auto-detect | code-search provider: `voyage` (Voyage 4 Large), `voyage-context`, `voyage-code-3`, `jina`, or `local` |
| `VOYAGE_API_KEY` | Only for Voyage | - | Enables Voyage in code-search and optional Voyage embeddings in code-graph |
| `CODE_GRAPH_SKIP_EMBEDDINGS` | No | Unset | Set to `1` or `true` to prevent code-graph from generating Voyage node embeddings even if the key is present |
| `LOCAL_EMBEDDING_MODEL` | No | Provider-specific | HuggingFace model for `jina` or `local` provider |
| `JINA_TRUNCATE_DIM` | No | - | Matryoshka dim truncation (0.5b: 64-896, 1.5b: 128-1536) |
| `QUANTIZATION` | No | `int8` | FAISS index type: `int8` (4x smaller), `float32`, `binary` (32x smaller) |

## Usage

### Step 1: Integrated indexing

```
/index-repo /path/to/your/monorepo
```

The command indexes code-search first, verifies semantic completion, indexes
code-graph with `skip_report=true`, and then requires both engines to report
ready with matching complete v1 checkout identities.

**Component timing reference** (3,000 chunks, typical single crate/package):

| Provider | Time | Notes |
|----------|------|-------|
| `voyage-context` | ~5-10 min | API calls, rate-limited |
| `voyage` | ~5-10 min | API calls, rate-limited |
| `jina` | ~50 min (CPU) | Local, no API. First run downloads ~1GB model |
| `local` | ~2-5 min | Local, small model, lower quality |

Code-graph's AST extraction and SQLite graph construction are local and
typically complete in ~30-60 seconds. If `VOYAGE_API_KEY` is present and graph
embeddings are not disabled, code-graph also sends selected node text to
Voyage; that optional pass adds API-dependent time and cost.

### Step 2: Search (after a verified dual index)

| Question type | Example | What happens |
|--------------|---------|-------------|
| **Conceptual** | "How does authentication work?" | Semantic search finds auth code, graph traces the call chain |
| **Structural** | "What calls processOrder?" | Graph traces inbound call path with risk classification |
| **Discovery** | "Find the rate limiting implementation" | Semantic search by meaning, not just keywords |
| **Architecture** | "Show all API endpoints" | Graph queries for Route/Handler nodes |
| **Impact** | "Blast radius of changing UserService?" | Graph traces all dependents with hop-distance risk |
| **Quality** | "Find dead code" | Graph finds functions with zero inbound calls |
| **Security** | "Where are the input entry points / auth boundaries?" | Graph queries security-tagged surfaces (auth/crypto/input/sink) |
| **Security** | "Does user input reach a sensitive sink?" | Graph traces source→sink taint paths |
| **Compliance** | "What code satisfies STIG control X?" | Graph maps the control ID to code evidence |
| **Localization** | "Where would I fix \<issue\>?" | Semantic chunk evidence is aggregated into a file-level ranking |

### Step 3: Multi-repo

Each verified repo can be activated independently.

```
/index-repo /path/to/repo-a
/index-repo /path/to/repo-b
# Now asking about repo-a's code auto-switches back to repo-a
```

## Historical component-only measurements

The table below predates the provenance-bound routing/evidence harness. It
compares embedding providers inside code-search on 102 historical queries.
This is not an integrated E2E comparative grade and must not be presented as a
current live plugin result.

| Provider | Model | MRR (Nix) | MRR (Rust svc) | MRR (Rust lib) | MRR (TypeScript) | Data leaves machine? | Cost |
|----------|-------|-----------|----------------|----------------|------------------|---------------------|------|
| **`voyage-context`** | voyage-context-3 | **0.723** | **0.783** | **0.861** | **0.677** | Yes | ~$0.06/1M tokens |
| historical `voyage` selector | voyage-code-3 | 0.584 | 0.742 | 0.861 | 0.642 | Yes | ~$0.06/1M tokens |
| **`jina` (enriched)** | jina-code-embeddings-0.5b | **0.638** | 0.742 | ~0.86 | **0.660** | **No** | **Free** |
| `jina` (baseline) | jina-code-embeddings-0.5b | 0.582 | 0.742 | ~0.86 | 0.660 | No | Free |
| `local` | all-MiniLM-L6-v2 | ~0.35 | ~0.45 | ~0.50 | ~0.40 | No | Free |

*Jina "enriched" = default mode. Prepends sibling chunk names to each chunk's header, approximating Voyage's contextualized embeddings. Enabled automatically for Jina and local providers.*

At the time of this measurement, the recorded `voyage` selector resolved to
`voyage-code-3`. That historical label is not the current `voyage` provider:
at the pinned code-search release, `voyage` maps to `voyage-4-large`, while
`voyage-code-3` is a separately selected non-default provider.

### Key findings

- **In this historical run, `voyage-context-3` led the tested models.** Its
  advantage came from embedding chunks with awareness of their file context
  (sibling chunks). The advantage was largest for declarative configuration
  languages (+24% on Nix) and smallest for self-contained libraries (0% on
  Rust libs). This result does not compare the current `voyage-4-large`
  mapping.

- **`jina-code-0.5b` with enriched headers closes 40% of the gap to Voyage** on Nix (0.582 → 0.638, reference 0.723). Enriched context is on by default — no configuration needed. It runs entirely on-device with no API calls.

- **In this historical run, `jina` beat `voyage-code-3`** on Nix (0.638
  vs 0.584, +9.2%) and TypeScript (0.660 vs 0.642, +2.8%), while running
  locally and free of API cost.

- **In this historical run, `voyage-code-3` did not outperform Jina overall**
  and required sending code to Voyage. It remains a distinct non-default
  selector; use current component evidence when evaluating it for a specific
  corpus.

### Which should I use?

| Situation | Recommended provider |
|-----------|---------------------|
| Current cloud default for code | `voyage` (`voyage-4-large`) |
| Contextualized documentation indexing | `voyage-context` |
| Older `voyage-code-3`-specific workflow | `voyage-code-3` (explicitly; not `voyage`) |
| Code must stay on-device (security/compliance) | `jina`, remove the graph Voyage key, and set `CODE_GRAPH_SKIP_EMBEDDINGS=1` as defense in depth |
| Quick evaluation, don't want to set up API keys | `jina` |
| Smallest possible index, lowest resource usage | `local` (lower quality) |

### Jina model variants

| Model | Params | Dim | RAM | CPU Index (3K chunks) |
|-------|--------|-----|-----|----------------------|
| `jinaai/jina-code-embeddings-0.5b` (default) | 494M | 896 | ~2.3 GB | ~50 min |
| `jinaai/jina-code-embeddings-1.5b` | 1.54B | 1536 | ~4 GB | ~120 min |

Set via `LOCAL_EMBEDDING_MODEL=jinaai/jina-code-embeddings-1.5b`. Both support Matryoshka dimension truncation via `JINA_TRUNCATE_DIM`.

## Supported Languages

**code-search** (semantic): Any text file. AST-aware chunking for Rust, Python, TypeScript, JavaScript, Go, Java, C, C++, C#, Nix, HCL (Terraform), TOML, YAML, and Markdown.

**code-graph** (structural): Rust, Python, TypeScript, JavaScript, Go, Java, C, C++, C#, Nix, HCL, Ruby, Swift, Kotlin, Scala, and more.

## Lessons Learned: prototype-software-merry

Practical advice from running this plugin on the Corsair monorepo (~4,800 files, 34K chunks, Rust/Nix/TypeScript/Python/HCL).
These are historical component observations and workflow guidance, not the
readiness evidence for the current BOM.

### Index by concern, not the entire repo

Indexing the entire monorepo at once produces 34K chunks. Queries compete against everything — a search for "firewall config" matches Nix modules, Rust network code, TypeScript UI components, and Terraform security groups. The results are diluted.

**Recommended workflow:** Index sub-projects separately based on what you're
working on:

```
/index-repo /path/to/monorepo/nix           # NixOS system config
/index-repo /path/to/monorepo/assetman      # Asset management service (Rust)
/index-repo /path/to/monorepo/libnet        # Networking library (Rust)
/index-repo /path/to/monorepo/mithrandir    # Web UI (TypeScript)
```

The active project auto-switches when you ask questions. If you're working on the Rust networking library, the results come from `libnet` — not the entire monorepo.

### Quality varies by language — context matters more for config languages

Our eval (102 queries, 4 languages) showed that embedding quality depends on the language:

| Language type | Example | voyage-context-3 advantage | Why |
|---|---|---|---|
| **Declarative config** | Nix, HCL | **+24%** over baseline | `allowedTCPPorts = [ 80 443 ]` is meaningless without file context |
| **Application services** | Rust services, TypeScript | **+5.5%** | Typed functions carry meaning in signatures, but service context helps |
| **Self-contained libraries** | Rust libraries | **0%** (tie) | `fn login(path: &Path) -> Result<()>` is fully self-descriptive |

**Practical implication:** If you work primarily in Rust libraries, Jina (free, local) gives you the same quality as Voyage. If you work in NixOS config or large service codebases, Voyage's contextualized embeddings are worth the API cost.

### Nix-specific: use full mode for code-graph

The `/index-repo` skill handles this automatically. When testing code-graph
directly, Nix repos require `mode: "full"`.

### First-time indexing is slow with Jina — incremental is fast

The Jina 0.5b model takes ~50 minutes to index 3K chunks on CPU (no GPU). This is a one-time cost. After the initial index:
- **Incremental re-index**: Only changed files are re-embedded. A 10-file change takes seconds.
- **Project switching**: Instant — just loads the existing index from disk.
- **Query latency**: ~5 seconds per query on CPU.

If the initial indexing time is a blocker, index during lunch or overnight.
Or use the current Voyage code provider for the initial index
(`EMBEDDING_PROVIDER=voyage`), then switch to Jina for daily use once you're
willing to re-index.

### The graph and semantic tools complement each other — don't use just one

Common mistake: using only semantic search ("find the auth code") and ignoring the graph. The graph answers questions that semantic search fundamentally cannot:

- **"What happens if I change this function?"** → Graph traces all callers (blast radius)
- **"Is this function dead code?"** → Graph checks for zero inbound calls
- **"What's the dependency chain from main() to this handler?"** → Graph traces the call path

Conversely, semantic retrieval is the preferred first step for a conceptual
question such as "where is the code that handles rate limiting?" Graph text
search can corroborate it, but it is not a substitute for the semantic FIND
route.

**Best workflow:** Start with semantic search when the structural target is not
yet known, then use graph queries to *understand* how it connects. If the user
already names an exact symbol and asks only for callers or dependencies, go
directly to the narrowest graph relationship tool.

### Versioned indexes for docs and release notes

Version-specific workflows can use isolated Git worktrees:

```bash
# Create worktrees for each version you want to index
git worktree add ../myrepo-v1 v1.0.0
git worktree add ../myrepo-v2 v2.0.0

# Index each version separately — different paths = different project IDs
/index-repo ../myrepo-v1
/index-repo ../myrepo-v2
```

Each version gets its own isolated index. Use `switch_project` (or just ask about code in a specific version) to query one or the other. The plugin auto-switches based on which project your question targets.

**Use cases:**
- **Release notes**: Index v1 and v2, search each for "what changed in authentication" — compare results
- **Migration guides**: Index old and new versions, find functions that moved or were renamed
- **Version-specific docs**: Index the exact code a version ships, generate docs from that snapshot — no hallucinations from newer code

**Tip for doc generation:** If your docs are well-structured (MDX, Markdown with clear sections), the local Jina model works well — structured text is self-descriptive, like typed code. You don't need Voyage's contextualized embeddings for docs that already have good headings and organization.

**Current limitations:**
- No cross-project search (can't query both versions in one call)
- No diff between indexes ("show what changed between v1 and v2")
- Worktrees use disk space for each checked-out version

## Troubleshooting

**"Search returns irrelevant results from wrong files"**
- Check which verified project is active and rerun `/index-repo` for a stale
  or unverified checkout.

**"Indexing seems stuck"**
- Jina CPU indexing is genuinely slow (~1 chunk/second on CPU). Check `~/.claude_code_search/` for growing index files.
- For Voyage, check your API key is valid and you haven't hit rate limits.

**"All search results come from one large file"**
- Some repos have very large files (generated code, lockfiles) that dominate results. Add them to `.gitignore` or index a sub-directory instead.

**"code-graph returns 0 nodes"**
- For Nix repos: ensure `mode: "full"` is used (the `/index-repo` skill does this automatically).
- Check that the target directory has files in supported languages.

**"Vector search results seem random"**
- If you indexed before 2026-04-05: the FAISS int8 quantizer had a bug that returned zero similarities. Re-index to fix.

## Notes

- **Incremental updates**: Re-run `/index-repo`; code-search re-embeds changed
  files and both engines must re-verify the checkout identity.
- **Index storage**: Indexes are stored in `~/.claude_code_search/` by default. Set `CODE_SEARCH_STORAGE` to change.
- **code-graph privacy**: Structural extraction and SQLite storage are local.
  With `VOYAGE_API_KEY`, code-graph can send selected node and query text to
  Voyage for embedding-backed features. Remove the key before launching the
  MCP to prevent all graph Voyage calls. `CODE_GRAPH_SKIP_EMBEDDINGS=1`
  prevents node-embedding generation but does not by itself prevent query
  embedding against a pre-existing graph index while the key remains present.
