# LLM API Fixes

Read-only Python and Node.js scripts that find OpenAI and Anthropic problems through the API — retired models, runaway spend, quota exhaustion misread as a rate limit, and prompt caching that never pays back. They report and print the repair; they never write.

Every script here is read only. They hold a credential to a live account, so none of them writes: each one reads through the API, reports exactly what is wrong, and prints the repair for you to run.

By **[Allan Niñal](https://github.com/allanninal)** — AI Solutions Engineer. I build AI powered tools, data products, and AWS automation.
Full write ups with diagrams for each fix live at **[allanninal.dev/llm](https://www.allanninal.dev/llm/)**.

[![Follow on GitHub](https://img.shields.io/github/followers/allanninal?label=Follow%20%40allanninal&style=social)](https://github.com/allanninal)
## The fixes

- [an archived project still holds live API keys](./archived-project-still-holds-keys/) — https://www.allanninal.dev/llm/archived-project-still-holds-keys/
- [audio and image usage never shows up in a token dashboard](./audio-and-image-line-items-unnoticed/) — https://www.allanninal.dev/llm/audio-and-image-line-items-unnoticed/
- [scheduled jobs pay full price for work the Batch API halves](./batch-discount-left-unused/) — https://www.allanninal.dev/llm/batch-discount-left-unused/
- [the batch left an error_file_id that nothing ever fetched](./batch-error-file-never-read/) — https://www.allanninal.dev/llm/batch-error-file-never-read/
- [a batch expired when the 24 hour completion window closed](./batch-expired-past-24h-window/) — https://www.allanninal.dev/llm/batch-expired-past-24h-window/
- [a batch reads completed while some of its rows failed](./batch-partial-failure-unnoticed/) — https://www.allanninal.dev/llm/batch-partial-failure-unnoticed/
- [Cache written on every call by a prefix that keeps moving](./cache-invalidated-by-changing-prefix/) — https://www.allanninal.dev/llm/cache-invalidated-by-changing-prefix/
- [cache writes are paid for and never read back](./cache-writes-with-no-reads/) — https://www.allanninal.dev/llm/cache-writes-with-no-reads/
- [code execution has spent its free 1,550 container hours](./code-execution-hours-exceed-free-allowance/) — https://www.allanninal.dev/llm/code-execution-hours-exceed-free-allowance/
- [fast mode billed at twice the rate and served as default](./fast-mode-silently-downgraded/) — https://www.allanninal.dev/llm/fast-mode-silently-downgraded/
- [a fine-tuned model was trained, billed, and never called once](./fine-tuned-model-never-used/) — https://www.allanninal.dev/llm/fine-tuned-model-never-used/
- [a floating model alias silently changes model under you](./floating-alias-instead-of-pinned-snapshot/) — https://www.allanninal.dev/llm/floating-alias-instead-of-pinned-snapshot/
- [a frontier model is answering twenty-token questions](./frontier-model-on-trivial-workload/) — https://www.allanninal.dev/llm/frontier-model-on-trivial-workload/
- [ITPM runs out because uncached input is never cached](./itpm-exhausted-uncached-input/) — https://www.allanninal.dev/llm/itpm-exhausted-uncached-input/
- [keys still work after their owner loses project access](./key-owner-lost-project-access/) — https://www.allanninal.dev/llm/key-owner-lost-project-access/
- [A live project's usage buckets have been empty for days](./live-project-zero-usage-buckets/) — https://www.allanninal.dev/llm/live-project-zero-usage-buckets/
- [most of your input tokens sit in the 200k-1M band](./long-context-requests-unwatched/) — https://www.allanninal.dev/llm/long-context-requests-unwatched/
- [max_tokens is set above the model's own output cap](./max-tokens-above-model-cap/) — https://www.allanninal.dev/llm/max-tokens-above-model-cap/
- [a model id in use is past its published shutdown date](./model-past-shutdown-date/) — https://www.allanninal.dev/llm/model-past-shutdown-date/
- [a model you still call retires in under 90 days](./model-retiring-within-90-days/) — https://www.allanninal.dev/llm/model-retiring-within-90-days/
- [no hard spend limit is set, so the bill has no ceiling](./no-organization-spend-limit/) — https://www.allanninal.dev/llm/no-organization-spend-limit/
- [A non-streaming request over 10 minutes times out with 504](./non-streaming-request-over-ten-minutes/) — https://www.allanninal.dev/llm/non-streaming-request-over-ten-minutes/
- [one line item or project is most of the organization's bill](./one-model-or-project-dominates-cost/) — https://www.allanninal.dev/llm/one-model-or-project-dominates-cost/
- [output tokens per minute is the real ceiling, not RPM](./otpm-exhausted/) — https://www.allanninal.dev/llm/otpm-exhausted/
- [output tokens, not input, are what the bill is made of](./output-tokens-dominate-cost/) — https://www.allanninal.dev/llm/output-tokens-dominate-cost/
- [529 overloaded errors arrive in clusters and get dropped](./overloaded-529-clusters/) — https://www.allanninal.dev/llm/overloaded-529-clusters/
- [Parallel tool calls void the strict schema guarantee](./parallel-tool-calls-with-strict-schema/) — https://www.allanninal.dev/llm/parallel-tool-calls-with-strict-schema/
- [per-customer cost is unknowable because tenants share a key](./per-tenant-cost-attribution-impossible/) — https://www.allanninal.dev/llm/per-tenant-cost-attribution-impossible/
- [prompt caching was never switched on anywhere](./prompt-caching-never-used/) — https://www.allanninal.dev/llm/prompt-caching-never-used/
- [Prompts overflow the context window and 400 as too long](./prompt-too-long-context-overflow/) — https://www.allanninal.dev/llm/prompt-too-long-context-overflow/
- [429 credit_balance_exhausted retried forever as a rate limit](./quota-exhausted-not-rate-limited/) — https://www.allanninal.dev/llm/quota-exhausted-not-rate-limited/
- [429s are retried blindly without reading which limit hit](./rate-limit-429-limiter-unidentified/) — https://www.allanninal.dev/llm/rate-limit-429-limiter-unidentified/
- [x-ratelimit-remaining sits near zero before any 429](./rate-limit-headers-near-exhaustion/) — https://www.allanninal.dev/llm/rate-limit-headers-near-exhaustion/
- [Requests billed, zero output tokens: max_tokens refused](./reasoning-model-rejects-max-tokens/) — https://www.allanninal.dev/llm/reasoning-model-rejects-max-tokens/
- [reasoning tokens are billed as output but never returned](./reasoning-tokens-billed-invisibly/) — https://www.allanninal.dev/llm/reasoning-tokens-billed-invisibly/
- [The model refused and the refusal field was never read](./refusal-field-ignored/) — https://www.allanninal.dev/llm/refusal-field-ignored/
- [A 32 MB request is rejected with 413 before Anthropic sees it](./request-too-large-413/) — https://www.allanninal.dev/llm/request-too-large-413/
- [Request count tripled while token volume stayed flat](./requests-diverge-from-token-volume/) — https://www.allanninal.dev/llm/requests-diverge-from-token-volume/
- [a retired model id still sitting in the code](./retired-model-id-still-in-code/) — https://www.allanninal.dev/llm/retired-model-id-still-in-code/
- [spend jumped week over week and no release explains it](./spend-spike-week-over-week/) — https://www.allanninal.dev/llm/spend-spike-week-over-week/
- [streamed responses report no usage and the dashboard undercounts](./streaming-usage-lost/) — https://www.allanninal.dev/llm/streaming-usage-lost/
- [strict omitted, so the JSON schema is only a suggestion](./strict-false-schema-silently-ignored/) — https://www.allanninal.dev/llm/strict-false-schema-silently-ignored/
- [JSON cut off mid-object because the ceiling was reached](./structured-output-truncated-by-length/) — https://www.allanninal.dev/llm/structured-output-truncated-by-length/
- [Tool-call arguments that parse and still break the schema](./tool-call-arguments-unparseable/) — https://www.allanninal.dev/llm/tool-call-arguments-unparseable/
- [Tool shipped on every request and never once called](./tool-defined-but-never-called/) — https://www.allanninal.dev/llm/tool-defined-but-never-called/
- [Tool schemas are most of the input tokens on every call](./tool-schemas-dominate-input-tokens/) — https://www.allanninal.dev/llm/tool-schemas-dominate-input-tokens/
- [US inference geo is billing every token at 1.1x](./us-inference-geo-premium-unnoticed/) — https://www.allanninal.dev/llm/us-inference-geo-premium-unnoticed/
- [web search is billing $10 per 1,000 searches unnoticed](./web-search-spend-unnoticed/) — https://www.allanninal.dev/llm/web-search-spend-unnoticed/

## How to run one

Each folder holds the same script in Python and in Node.js, plus its test. Set the environment variables named in that folder's README and run it. Nothing writes, so there is no dry run to enable and no flag to be careful about — use a restricted, read-only credential and the worst case is that it tells you nothing is wrong.

## License

MIT. Use it, change it, ship it.
