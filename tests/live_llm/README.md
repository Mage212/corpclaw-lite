# Live LLM tests

Manual tests for real llama.cpp / llama-server behavior: concurrency, slots,
persistent KV-cache save/restore, validation, and agent-scoped cache identity.

These tests are intentionally excluded from the normal test pool.

## Required environment

```bash
export CORPCLAW_LIVE_LLM_TESTS=1
export CORPCLAW_LIVE_LLM_BASE_URL=http://192.168.193.178:8080
export CORPCLAW_LIVE_LLM_MODEL=gpt-oss-20b-UD-Q4_K_XL
export CORPCLAW_LIVE_LLM_API_KEY=dummy
export CORPCLAW_LIVE_LLM_SLOTS=0,1,2,3
export CORPCLAW_LIVE_LLM_PROMPT_TOKENS=1000
export CORPCLAW_LIVE_LLM_LARGE_PROMPT_TOKENS=5000
export CORPCLAW_LIVE_LLM_MAX_TOKENS=24
```

Optional:

```bash
export CORPCLAW_LIVE_LLM_CACHE_ROOT=/home/vadim/llama-router-models/slot-cache/gpt-oss-20b
export CORPCLAW_LIVE_LLM_KEEP_CACHE=1
export CORPCLAW_LIVE_LLM_RUN_SLOW=1
```

`CORPCLAW_LIVE_LLM_CACHE_ROOT` is only needed if CorpClaw can see the same
server-side directory used by llama-server `--slot-save-path`. Without it, tests
can erase live slots but cannot delete server-side cache files.

## Commands

```bash
CORPCLAW_LIVE_LLM_TESTS=1 uv run pytest tests/live_llm/ -v -s -o addopts=''
CORPCLAW_LIVE_LLM_TESTS=1 uv run pytest tests/live_llm/test_02_cache_file_roundtrip.py -v -s -o addopts=''
CORPCLAW_LIVE_LLM_TESTS=1 uv run pytest tests/live_llm/test_04_parallel_slots.py -v -s -o addopts=''
CORPCLAW_LIVE_LLM_TESTS=1 CORPCLAW_LIVE_LLM_RUN_SLOW=1 uv run pytest tests/live_llm/ -v -s -o addopts=''
```

`-o addopts=''` is required because `tests/live_llm` is ignored by default in
`pyproject.toml`.

## Reports

Each test writes JSON reports under:

```text
reports/live_llm/<timestamp>/
```

Reports include model, slots, prompt metrics, cache reuse ratio, TTFT, save and
restore timings, and raw summarized slot action results.

## Safety

- Test cache files use the `corpclaw_live_` prefix.
- Tests only erase slots listed in `CORPCLAW_LIVE_LLM_SLOTS`.
- Tests never delete files without the managed prefix.
- Generation is capped by `CORPCLAW_LIVE_LLM_MAX_TOKENS` because these tests
  measure prompt processing and cache behavior, not long-form answer quality.
