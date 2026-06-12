# Model Catalog

All models available through the NinjaTech LiteLLM gateway.

**Gateway URL**: `https://model-gateway.public.beta.myninja.ai`
**Auth**: Bearer token from `/root/.claude/settings.json`

---

## Chat / Text Models

These models accept chat completion requests via `/v1/chat/completions`.

| Alias | Full Model ID | Provider | Best For | Verified |
|-------|---------------|----------|----------|----------|
| `claude-opus` | `claude-opus-4-8` | Anthropic | **Default.** Complex reasoning, coding, long-horizon agents | ✅ |
| `claude-opus-4-8` | `claude-opus-4-8` | Anthropic | Explicit alias for the latest Opus | ✅ |
| `claude-opus-4-7` | `claude-opus-4-7` | Anthropic | Previous-generation Opus (kept for migration) | ✅ |
| `claude-opus-4-6` | `claude-opus-4-6` | Anthropic | Previous-generation Opus (kept for migration) | ✅ |
| `claude-sonnet` | `claude-sonnet-4-6` | Anthropic | Balanced quality/speed at ~40% of Opus cost | ✅ |
| `claude-sonnet-4-6` | `claude-sonnet-4-6` | Anthropic | Explicit alias | ✅ |
| `claude-haiku` | `claude-haiku-4-5-20251001` | Anthropic | Fast responses, simple tasks | ✅ |
| `gpt-5` | `openai/openai/gpt-5.5` | OpenAI | General purpose, strong coding; reasoning model | ✅ |
| `gpt-5.5` | `openai/openai/gpt-5.5` | OpenAI | Explicit alias for the current flagship | ✅ |
| `gpt-5.4` | `openai/openai/gpt-5.4` | OpenAI | Previous-generation GPT-5 (still available) | ✅ |
| `gemini-pro` | `google/gemini/gemini-3-pro-preview` | Google | Multimodal, long context | ✅ |
| `ninja-fast` | `ninja-cline-fast` | NinjaTech | Quick agent tasks | ✅ |
| `ninja-standard` | `ninja-cline-standard` | NinjaTech | Standard agent tasks | ✅ |
| `ninja-complex` | `ninja-cline-complex` | NinjaTech | Complex agent tasks | ✅ |

### Choosing a Chat Model

- **Default for most production workflows** → `claude-opus` (= `claude-opus-4-8`)
- **Need speed / low cost?** → `claude-haiku` or `ninja-fast`
- **Balanced quality/price?** → `claude-sonnet` (~40% cheaper than Opus, same context window)
- **Agent tasks (Cline/autonomous workflows)?** → `ninja-complex`

### Opus 4.7 Migration Notes (from Opus 4.6)

Three breaking changes in Opus 4.7 (per Anthropic's migration guide) that may affect existing code:

1. **Extended thinking is removed.** `thinking: {"type": "enabled", ...}` returns 400. Use adaptive thinking (`thinking: {"type": "adaptive"}`) controlled by the `effort` parameter (`low` / `standard` / `high` / `xhigh` / `max`).
2. **`temperature`, `top_p`, `top_k` are locked.** Setting any to a non-default value returns 400. Guide behavior via prompting instead.
3. **Thinking content is hidden by default.** Streamed thinking blocks have an empty `thinking` field unless you set `"display": "summarized"`.

Same $5 / $25 per-million-token rate as 4.6, but the new tokenizer produces **up to 35% more tokens** on the same text (worst case on code and structured data). Benchmark real traffic before migrating cost-sensitive workloads.

---

## Image Generation Models

These models accept image generation requests via `/v1/images/generations` and image
edit / multi-reference composition requests via `/v1/images/edits`.

| Alias | Full Model ID | Provider | Verified |
|-------|---------------|----------|----------|
| `gpt-image` | `alias/openai/gpt-image-2.0` | OpenAI | ✅ **Default** — state-of-the-art |
| `gpt-image-2` | `alias/openai/gpt-image-2.0` | OpenAI | ✅ Explicit alias for latest |
| `gpt-image-1.5` | `openai/openai/gpt-image-1.5` | OpenAI | ✅ Legacy (kept for migration) |
| `gemini-image` | `google/gemini/gemini-3-pro-image-preview` | Google | ✅ |

### Choosing an Image Model

- **Default for new work** → `gpt-image` (= `gpt-image-2`). Highest quality,
  supports text rendering, multi-reference composition, and flexible sizes.
- **Legacy workflows** → `gpt-image-1.5`. Keep during validation only.
- **Alternate provider** → `gemini-image`. Fast, but **ignores `size`** and
  returns its own aspect ratio (JPEG, non-standard dimensions).

### gpt-image-2 Capabilities

| Capability | Notes |
|---|---|
| Resolution | Any res up to 2K stable, 2K–4K experimental. Max edge < 3840, multiples of 16, ratio ≤ 3:1, 655K ≤ pixels ≤ 8.3M |
| Reference images | **Up to 16** in a single `/v1/images/edits` call |
| Text in images | Crisp, multilingual. Put literal strings in quotes or ALL CAPS. |
| `quality` | `low` / `medium` / `high`. Low is fast; high for dense text/infographics. |
| `input_fidelity` | **Not supported** (output is high-fidelity by default) |
| Output format | PNG URL (downloaded by the utility) |

### Popular `gpt-image-2` Sizes

| Label | Resolution | Notes |
|-------|------------|-------|
| Square | `1024x1024` | Good general-purpose default |
| HD portrait | `1024x1536` | Standard portrait |
| HD landscape | `1536x1024` | Standard landscape |
| 2K square | `2048x2048` | Experimental upper reliability boundary |
| Auto | `"auto"` | Let the model choose (returns ~1254x1254) |

### Image Prompting Fundamentals

1. **Structure**: `[Subject + adjectives] doing [Action] in [Scene/Context]. [Composition/Camera]. [Lighting/Atmosphere]. [Style/Medium]. [Exact Text]. [Aspect Ratio].`
2. **Reference indexing**: For multi-ref edits, name each input in the prompt — `"Image 1: <desc>... Image 2: <desc>..."` — and describe how they interact (`"apply Image 2's style to Image 1"`, `"place the cat from Image 2 on the chair from Image 1"`).
3. **Literal text**: Put exact in-image text in **quotes** or **ALL CAPS**. For tricky words, spell them out letter-by-letter.
4. **Preserve list on edits**: State invariants explicitly (`"keep the face, pose, background, and brand logo unchanged"`). Repeat the preserve list on every iteration to prevent drift.
5. **Iterate small**: A base prompt + small single-change follow-ups beats one giant rewrite.

### Gateway Behavior Notes

- Responses return a signed **URL** to the generated PNG (not base64 by default).
- When you attach many reference images (≥ 4–6), the gateway may **auto-downgrade `quality` to `low`** to stay within capacity. This is harmless for drafts; for production, request fewer refs and retry with explicit `quality="medium"` or `"high"`.
- `output_format` parameter is accepted but currently **always returns PNG** regardless of value.
- `background="transparent"` is accepted but does not consistently produce true alpha channels — verify after generation.

---

## Video Generation Models

These models use the async video workflow via `/v1/videos`.

| Alias | Full Model ID | Provider | Quality | Speed | Verified |
|-------|---------------|----------|---------|-------|----------|
| `sora` | `openai/openai/sora-2` | OpenAI | Standard | ~90s | ✅ |
| `sora-pro` | `openai/openai/sora-2-pro` | OpenAI | High | ~120s | ✅ |

### Video Sizes

- `1280x720` — Landscape (16:9)
- `720x1280` — Portrait (9:16)

### Video Parameters

- **Max duration**: 8 seconds
- **Generation time**: 60-120 seconds typically
- **Output format**: MP4

### Video Workflow

Video generation is **asynchronous** (3-step process):
1. `POST /v1/videos` → Submit job, get `video_id`
2. `GET /v1/videos/{video_id}` → Poll status (queued → in_progress → completed)
3. `GET /v1/videos/{video_id}/content` → Download MP4

**Important**: Status and content endpoints require the header `custom-llm-provider: openai`.

---

## Embedding Models

These models accept embedding requests via `/v1/embeddings`.

| Alias | Full Model ID | Provider | Dimensions | Verified |
|-------|---------------|----------|------------|----------|
| `embed-small` | `openai/openai/text-embedding-3-small` | OpenAI | 1,536 | ✅ |
| `embed-large` | `openai/openai/text-embedding-3-large` | OpenAI | 3,072 | ✅ |

### Choosing an Embedding Model

- **`embed-small`** — Good for most use cases, lower cost, 1536 dimensions
- **`embed-large`** — Higher accuracy, better for semantic search, 3072 dimensions

### Use Cases

- Semantic search and retrieval
- Document similarity comparison
- Clustering and classification
- RAG (Retrieval-Augmented Generation)

---

## Model Aliases

The utility library supports short aliases. Use `resolve_model()` to convert:

```python
from utils.litellm_client import resolve_model

resolve_model("claude-opus")    # → "claude-opus-4-8"
resolve_model("claude-sonnet")  # → "claude-sonnet-4-6"
resolve_model("gpt-5")          # → "openai/openai/gpt-5.5"
resolve_model("sora")           # → "openai/openai/sora-2"
resolve_model("embed-small")    # → "openai/openai/text-embedding-3-small"

# Full IDs are passed through unchanged
resolve_model("claude-opus-4-8")  # → "claude-opus-4-8"
```

---

## Rate Limits & Best Practices

1. **Retry on transient errors** — Gateway may return 500 for temporary issues
2. **Use appropriate models** — Don't use `claude-opus` for simple tasks
3. **Batch embeddings** — Use `embed_batch()` instead of multiple `embed()` calls
4. **Video polling** — Use 5-second intervals, don't poll too aggressively
5. **Image retries** — If `gpt-image` returns a transient error, retry the same call (up to 2×) before falling back to `gemini-image`
