# capped-video-mcp

A local MCP server that lets agents (Claude Code, Claude Desktop, …) generate videos through **Replicate** and **fal**, using your own API keys — with hard spending caps enforced by the server.

Supported models: `seedance-2.5` (Replicate, default), `seedance-2.5-fal`, `kling-3.0`, `wan-2.5`, `veo-3.1-fast`, `veo-3.1-lite`, `hailuo-2.3`, `gemini-omni-1.2-flash`. Each model has its own allowed durations, resolutions and prices (see `list_models` or the [default config.toml](https://github.com/bslim1009/capped-video-mcp/blob/main/src/capped_video_mcp/config.toml)).

Every model also has an **image-to-video** variant with the `-i2v` suffix (`seedance-2.5-i2v` is the default) that animates your own image as the first frame — see [Image to video](#image-to-video). Seedance 2.5 also has a **reference-to-video** mode (`seedance-2.5-ref`) that combines several of your images and audio clips into a new scene — see [Reference to video](#reference-to-video).

The server enforces cost limits (model allowlist, resolution/duration caps, daily and monthly budgets, concurrent jobs), detects duplicate requests so agent retries don't get billed twice, and downloads finished videos automatically.

> **Alpha.** Prices in the config are **estimates** used for budgeting (taken from provider list prices, checked 2026-11-22). Check actual charges on your Replicate/fal dashboard. Provider request formats were validated against the public API schemas, but end-to-end verification of real generation and download is still in progress. Start with the cheapest settings (480p, 4–5 s) and a low budget cap.

## Install and register

Requirements: [uv](https://docs.astral.sh/uv/) and a Replicate token and/or a fal key (one is enough; models of a provider without a key are disabled). Optional: [ffmpeg](https://ffmpeg.org/), used only by `preview_video`.

### Claude Code

```bash
claude mcp add capped-video --scope user \
  -e FAL_KEY=fal-xxxx \
  -e REPLICATE_API_TOKEN=r8_xxxx \
  -e CAPPED_VIDEO_DEFAULT_MODEL=kling-3.0 \
  -e CAPPED_VIDEO_DAILY_MAX_USD=3 \
  -- uvx capped-video-mcp
```

`--scope user` makes the server available in every project. To share a `.mcp.json` in a team repository, don't write keys into it; reference each developer's environment variables instead:

```json
{
  "mcpServers": {
    "capped-video": {
      "command": "uvx",
      "args": ["capped-video-mcp"],
      "env": {
        "FAL_KEY": "${FAL_KEY:-}",
        "REPLICATE_API_TOKEN": "${REPLICATE_API_TOKEN:-}",
        "CAPPED_VIDEO_DEFAULT_MODEL": "kling-3.0"
      }
    }
  }
}
```

Check the connection with `/mcp`. `generate_video` costs money — keep it out of any auto-allow list and approve each call.

`uvx` keeps using the version it first downloaded. To upgrade, register with `uvx capped-video-mcp@latest` or run `uvx --refresh capped-video-mcp` once.

### Claude Code plugin (prompts for keys and budget)

```
/plugin install capped-video-mcp --marketplace bslim1009/capped-video-mcp
```

Claude Code asks for your fal/Replicate keys, default model and budget caps when the plugin is enabled, stores keys in the OS credential store, and runs the server with `uvx capped-video-mcp`. Change the values later under `/config`.

### Claude Desktop extension (double-click install)

Download `capped-video-mcp-<version>.mcpb` from the [releases page](https://github.com/bslim1009/capped-video-mcp/releases) and open it with Claude Desktop. The install dialog asks for your keys, default model, budget caps and output folder; Claude Desktop manages Python and dependencies itself (UV runtime). No terminal needed.

### Other clients

Add the same JSON shape to `mcpServers` in your client's MCP settings (for Claude Desktop without the extension: `claude_desktop_config.json`). Write the key values directly instead of `${...}`.

### Try it without keys

Pass `CAPPED_VIDEO_PROVIDER=mock` and the server never calls a real API; it produces fake files and keeps its records separate (`data/usage-mock.json`, `<output dir>/mock/`).

## Tools

| Tool | Billed | What it does |
|---|---|---|
| `generate_video` | **yes** | Creates a generation job and returns a `job_id`. `model`, `resolution`, `duration_sec` and `aspect_ratio` can be chosen per request. Pass `image_path` (absolute path to a local image) for image-to-video, or `reference_image_paths` / `reference_audio_paths` / `reference_dir` for reference-to-video. **An identical request within the last 10 minutes does not create a new job; it returns the existing `job_id` with `duplicate: true`** (no charge). `request_id` also deduplicates; use `force: true` only when you really want a second copy |
| `get_video_status` | no | Job status. Waits up to 60 s. Returns the file path when finished |
| `download_video` | no | Retry the download if the automatic one failed |
| `get_usage` | no | Today's and this month's usage against the caps |
| `estimate_video` | no | Estimated cost per model for the given constraints (duration, resolution, aspect ratio), cheapest first, with remaining budget. Includes the cheapest model that fits the budget (`cheapest`), models that can't satisfy the constraints and why (`unavailable`), and a recent identical request if any (`duplicate_of`). With `image_path` it compares image-to-video models only; with reference files it compares reference-to-video models and returns which file is `[Image1]`, `[Audio1]`, … (`references`) |
| `preview_video` | no | Returns 6 frames of a finished video tiled into one **image**, so the agent can look at the result and decide whether to regenerate. Needs ffmpeg (returns the reason if missing) |
| `list_models` | no | Allowed models, prices, duration ranges and current defaults |

Results are collected only while the server (your client session) is running. Don't close the session while a generation is in progress.

### Image to video

Pass `image_path` with an **absolute** path to a PNG, JPEG or WebP file (up to 20 MB, `limits.max_image_bytes`). The image becomes the first frame. Without `model`, `defaults.image_model` (`seedance-2.5-i2v`) is used; text-only models reject images and `-i2v` models require one.

| Model | Endpoint | Aspect ratio |
|---|---|---|
| `seedance-2.5-i2v` | Replicate `bytedance/seedance-2.5` (`image`) | follows the image |
| `seedance-2.5-fal-i2v` | fal `bytedance/seedance-2.5/image-to-video` | follows the image |
| `kling-3.0-i2v` | fal `fal-ai/kling-video/v3/standard/image-to-video` (`start_image_url`) | follows the image (min 300 px, ratio 0.4–2.5) |
| `wan-2.5-i2v` | fal `fal-ai/wan-25-preview/image-to-video` | follows the image |
| `veo-3.1-fast-i2v`, `veo-3.1-lite-i2v` | fal `fal-ai/veo3.1/{fast,lite}/image-to-video` | follows the image (720p+ 16:9 or 9:16 recommended) |
| `hailuo-2.3-i2v` | fal `fal-ai/minimax/hailuo-2.3/standard/image-to-video` | follows the image |
| `gemini-omni-1.2-flash-i2v` | fal `google/gemini-omni-flash/v1.2/image-to-video` | `16:9` or `9:16` |

Prices are the same as the text-to-video versions. For models whose ratio follows the image, don't pass `aspect_ratio` (`list_models` shows `auto`).

Right before the generation request, the server uploads the image to the provider's storage and passes the resulting URL: Replicate's Files API (files expire after 24 hours) or the fal CDN (expiry set to 24 hours; URLs are unguessable but public by default). Upload failures happen before anything billable, so they are recorded as `create_failed` and can fail over (`seedance-2.5-i2v` ↔ `seedance-2.5-fal-i2v`). The job record and audit log keep the image path and SHA-256, never the image bytes.

### Reference to video

Give Seedance 2.5 several images (characters, objects, backgrounds) and optionally audio (voice, roars, music), and describe the scene. Refer to the files in the prompt as `[Image1]`, `[Image2]`, `[Audio1]`, … in the order they were given:

```text
generate_video(
  prompt="The dinosaur from [Image1] fights the dinosaur from [Image2] in a jungle, roaring like [Audio1]",
  reference_dir="C:/Project/Sample")
```

- `reference_image_paths` (PNG/JPEG/WebP, at least 1, up to 30) and `reference_audio_paths` (MP3/WAV, up to 10) take absolute paths in the order you choose. `reference_dir` takes a folder instead and uses its images and audio files sorted by file name (no subfolders, hidden files skipped). Use one or the other.
- Call `estimate_video` with the same arguments first (free): its `references` field shows which file became `[Image1]`, `[Audio1]`, … .
- Audio: each file at least 1.8 s, 30 s in total, up to 15 MB (`limits.max_audio_bytes`) — the intersection of both providers' limits, checked before anything is reserved.
- Models: `seedance-2.5-ref` (Replicate, default `defaults.reference_model`) and `seedance-2.5-fal-ref` (fal `bytedance/seedance-2.5/reference-to-video`, about 2× the price). They fail over to each other (switching to the pricier fal model needs the user's approval, see [Provider failover](#provider-failover)). Replicate writes references as `[Image1]` and fal as `@Image1`; the server rewrites either form to the target model's syntax.
- Reference mode can't be combined with `image_path` (first frame). Reference videos are not supported (video input costs about 4.2× more).
- Pricing: fal lists the same per-second price as image-to-video. Replicate's price table only distinguishes `video_in` from `non_video_in`; the config assumes image/audio references use the `non_video_in` rate.

### Duplicate request detection

Agents tend to resend a request when a response is slow or ambiguous. If a request with the same prompt (whitespace-normalized), model, resolution, duration, aspect ratio and input image content arrives within `duplicate_window_minutes` (default 10), the server returns the existing job's status instead of calling the provider. Rejected (`create_failed`, not billed) and failed (`failed`) jobs are not treated as duplicates, so they can be retried. The check and the reservation happen under the same file lock, so two clients sending the same request at once still create only one job. Disable with `CAPPED_VIDEO_DUPLICATE_WINDOW_MINUTES=0`.

### Cost confirmation before generating (optional)

Set a threshold such as `CAPPED_VIDEO_CONFIRM_ABOVE_USD=0.5` and, when the estimated cost is at or above it, **the server itself** asks the user "This will cost about $0.63. Proceed?" via MCP elicitation. This works independently of the client's tool-approval settings, so expensive generations get a second look even in auto-approve mode. If the client doesn't support elicitation, `CAPPED_VIDEO_CONFIRM_FALLBACK` applies (`allow`, the default, proceeds; `deny` refuses). The default `0` never asks.

### Provider failover

When the same model is available from two providers (`seedance-2.5` ↔ `seedance-2.5-fal`) and the first provider **definitively rejects** the request (4xx, maintenance, …), the server re-checks the remaining budget and generates with the fallback model; the response carries `failover_from`.

**The user decides when the fallback costs more** (e.g. `seedance-2.5` $0.51 → `seedance-2.5-fal` $1.10). A fallback that costs the same or less is used right away. A pricier one is used only after the server asks the user via MCP elicitation ("… rejected the request. Generating with seedance-2.5-fal costs about $1.10 (originally $0.51). Proceed?") and the user accepts; the reservation is kept alive while waiting for the answer. If the user declines, or the client doesn't support elicitation, nothing is generated or billed, and the error names the fallback model and its price so the agent can ask the user and retry with `model="<fallback>"`.

It never fails over on errors with an unknown outcome (timeouts, 5xx), because the first attempt may have been billed. Disable with `CAPPED_VIDEO_FAILOVER=false`; change the fallback with `fallback_model` in the config.

### Result preview

`preview_video` uses ffmpeg to tile 6 frames (3 × 2, in time order) into one JPEG and returns it as image content. The result is cached next to the video as `<job_id>.preview.jpg`. ffmpeg is looked up on PATH or via `CAPPED_VIDEO_FFMPEG`; if it's missing, the tool returns the video path and the reason. Frame count and width are set in the `[preview]` section of the config.

## Configuration

Settings are layered; later layers win:

1. Package defaults: [src/capped_video_mcp/config.toml](https://github.com/bslim1009/capped-video-mcp/blob/main/src/capped_video_mcp/config.toml)
2. `config.toml` in the data directory (optional; only the keys you want to change)
3. `CAPPED_VIDEO_*` environment variables (passed with `-e`/`env` at registration, or in `.env` in the data directory)

### Environment variables

| Variable | Setting | Example |
|---|---|---|
| `FAL_KEY` | fal API key (API scope — don't use an ADMIN key) | |
| `REPLICATE_API_TOKEN` | Replicate token | |
| `CAPPED_VIDEO_DEFAULT_MODEL` | `defaults.model` | `kling-3.0` |
| `CAPPED_VIDEO_DEFAULT_IMAGE_MODEL` | `defaults.image_model` (used when `image_path` is given without `model`) | `kling-3.0-i2v` |
| `CAPPED_VIDEO_DEFAULT_REFERENCE_MODEL` | `defaults.reference_model` (used when reference files are given without `model`) | `seedance-2.5-fal-ref` |
| `CAPPED_VIDEO_DEFAULT_RESOLUTION` | `defaults.resolution` | `480p` |
| `CAPPED_VIDEO_DEFAULT_DURATION_SEC` | `defaults.duration_sec` | `5` |
| `CAPPED_VIDEO_DEFAULT_ASPECT_RATIO` | `defaults.aspect_ratio` | `9:16` |
| `CAPPED_VIDEO_MAX_RESOLUTION` | `limits.max_resolution` | `720p` |
| `CAPPED_VIDEO_MAX_DURATION_SEC` | `limits.max_duration_sec` | `10` |
| `CAPPED_VIDEO_DAILY_MAX_COUNT` | `limits.daily_max_count` | `10` |
| `CAPPED_VIDEO_DAILY_MAX_USD` | `limits.daily_max_usd` | `5` |
| `CAPPED_VIDEO_MONTHLY_MAX_USD` | `limits.monthly_max_usd` | `30` |
| `CAPPED_VIDEO_MAX_PENDING_JOBS` | `limits.max_pending_jobs` | `3` |
| `CAPPED_VIDEO_MAX_IMAGE_BYTES` | `limits.max_image_bytes` (size cap per input image) | `20971520` |
| `CAPPED_VIDEO_MAX_AUDIO_BYTES` | `limits.max_audio_bytes` (size cap per reference audio file) | `15728640` |
| `CAPPED_VIDEO_DUPLICATE_WINDOW_MINUTES` | `limits.duplicate_window_minutes` (window for duplicate detection; `0` disables) | `10` |
| `CAPPED_VIDEO_CONFIRM_ABOVE_USD` | `limits.confirm_above_usd` (ask the user before generations costing at least this much; `0` disables) | `0.5` |
| `CAPPED_VIDEO_CONFIRM_FALLBACK` | `limits.confirm_fallback` (clients without elicitation: `allow`/`deny`) | `allow` |
| `CAPPED_VIDEO_FAILOVER` | `limits.failover` (switch to `fallback_model` on definitive rejection) | `true` |
| `CAPPED_VIDEO_FFMPEG` | `preview.ffmpeg` (ffmpeg path for previews; empty = PATH) | `C:/ffmpeg/bin/ffmpeg.exe` |
| `CAPPED_VIDEO_OUTPUT_DIR` | `download.dir` (where videos are saved; relative paths are resolved against the data directory) | `~/Videos/capped-video` |
| `CAPPED_VIDEO_HOME` | Data directory. **Absolute path**, registration env only (has no effect in `.env`) | `D:/capped-video` |
| `CAPPED_VIDEO_PROVIDER` | `mock` never calls a real API; any other value is an error | `mock` |

Empty values are treated as unset (a value in `.env` is used instead). Invalid values (unknown model or resolution, non-numeric budget, …) stop the server from starting and name the offending variable or file. Paths may use `~` and environment variables (`%USERPROFILE%`, `$HOME`). Never pass API keys as tool arguments or in chat — they would end up in transcripts and logs.

### Data directory

| How the server runs | Location |
|---|---|
| `CAPPED_VIDEO_HOME` set | that directory |
| Installed with `uvx`/`pip` | `~/.capped-video-mcp` (Windows: `C:\Users\<name>\.capped-video-mcp`) |
| Run from this repository after `uv sync` | repository root |

Contents: `config.toml` and `.env` (optional, created by you), `output/` (videos), `data/usage.json` (usage ledger), `logs/`.

Example data-directory `config.toml`. Tables (`[limits]`, …) are merged key by key; lists (`[[models]]`, `allowed_hosts`) are replaced as a whole.

```toml
[defaults]
model = "veo-3.1-lite"

[limits]
daily_max_usd = 2.0

[download]
dir = 'D:\Videos\capped-video'   # use single quotes or forward slashes for Windows paths
```

## Development

```bash
uv sync
cp .env.example .env        # fill in keys; or set CAPPED_VIDEO_PROVIDER=mock to run without keys
uv run pytest               # never calls a real API
```

To run the server from the repository, register it in `.mcp.json` (git-ignored):

```json
{
  "mcpServers": {
    "capped-video": {
      "command": "uv",
      "args": ["run", "--directory", "<repo path>", "python", "-m", "capped_video_mcp"]
    }
  }
}
```

Changes are listed on the [releases page](https://github.com/bslim1009/capped-video-mcp/releases).

## License

MIT

---

Official MCP Registry name (ownership marker for the PyPI package):

mcp-name: io.github.bslim1009/capped-video-mcp
