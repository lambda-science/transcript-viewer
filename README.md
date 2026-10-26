# transcript-viewer

> This is a personal fork that I am modifying to suit my own needs. It is not
> published to PyPI; install it directly from this repository.

Browse Claude Code, Codex, and Copilot transcripts in a local web viewer.
[`atif-make`](https://github.com/jammastergirish/atif-make) converts supported
logs to ATIF; this project provides the browser interface.

## Install or run

Run without installing:

```sh
uvx --from git+https://github.com/lambda-science/transcript-viewer.git transcript-viewer
uvx --from git+https://github.com/lambda-science/transcript-viewer.git transcript-viewer path/to/session.jsonl
```

Install the command permanently:

```sh
uv tool install git+https://github.com/lambda-science/transcript-viewer.git
```

Update an existing installation with `uv tool install --force` followed by the
same Git requirement.

## Usage

```sh
transcript-viewer                         # open the local library
transcript-viewer path/to/session.jsonl   # open one log
transcript-viewer bundle.zip              # open an archive
transcript-viewer s3://bucket/runs/session.json --profile my-profile
transcript-viewer --port 8080 --no-open
transcript-viewer sync s3://bucket/runs   # take what is new from S3
```

Opening an S3 object requires the `aws` CLI and an authenticated AWS profile.
Without `--profile`, the viewer uses its saved profile, `AWS_PROFILE`, or the
AWS CLI default. The download is temporary and removed when the viewer stops.

The viewer opens empty on a first run. Use **Add…** to import files, folders,
archives, GitHub repositories, and S3 locations. Remote repositories use a
picker that browses one level at a time and shows the measured download size
before fetching. The library groups sessions under derived Local and Remote
folders; **Clear library** removes the index without deleting source files.

The interface includes search and filters, Markdown rendering, tool-call and
subagent views, themes, tags, and favourites.

The session summary includes embedded trajectories and distinguishes recorded
steps, model calls, tool turns, and individual tool calls (including delegation).
Input and output totals include the cache and reasoning breakdowns shown beside
them. Missing metrics display as `—`; partial totals display as `≥`. Each model
step also shows its recorded input, output, cache reads, and cache writes.
NeMo Relay's empty DeepAgent request wrapper is hidden so the conversation opens
directly. Real subagent delegations remain visible; Raw retains the original file.
NeMo Relay ATIF exports from Pi and Deep-Agent both support tool outputs and
search, including their respective cache-write and reasoning token fields.
Pi exports using Chat Completions or Responses show earlier conversation history;
duplicate Responses model/hook tool calls display once with their results.

## Privacy and storage

The server binds `127.0.0.1` by default. Nothing is uploaded automatically and
there is no telemetry. Library data lives in `~/.transcript-viewer`; saved keys
are stored in `~/.transcript-viewer/config.json` inside a `0700` directory with
`0600` file permissions. Responses set `X-Content-Type-Options: nosniff`.

URL imports reject loopback, private, link-local, and reserved addresses,
including after redirects. S3 access uses the installed `aws` CLI and its
existing credentials. In a shared deployment, the identity header is trusted
only because it is named in `deploy.toml`; do not expose the server around the
authenticating proxy.

## Development

```sh
uv sync
uv run pytest
```

The page is re-read whenever it changes, at the cost of one stat per page load;
restart the server after Python changes.

Core files:

```text
src/transcript_viewer/
  cache.py
  cli.py
  config.py
  corpus.py
  db.py
  deploy.py
  fetch.py
  identity.py
  ingest.py
  library.py
  page.html
  store.py
  viewer.py
```

The server is standard-library Python plus one self-contained HTML page. There
is no frontend build step.
