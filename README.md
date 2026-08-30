# LLM API Fixes

Read-only Python and Node.js scripts that find OpenAI and Anthropic problems through the API — retired models, runaway spend, quota exhaustion misread as a rate limit, and prompt caching that never pays back. They report and print the repair; they never write.

Every script here is read only. They hold a credential to a live account, so none of them writes: each one reads through the API, reports exactly what is wrong, and prints the repair for you to run.

By **[Allan Niñal](https://github.com/allanninal)** — AI Solutions Engineer. I build AI powered tools, data products, and AWS automation.
Full write ups with diagrams for each fix live at **[allanninal.dev/llm](https://www.allanninal.dev/llm/)**.

[![Follow on GitHub](https://img.shields.io/github/followers/allanninal?label=Follow%20%40allanninal&style=social)](https://github.com/allanninal)
## The fixes

- [an archived project still holds live API keys](./archived-project-still-holds-keys/) — https://www.allanninal.dev/llm/archived-project-still-holds-keys/
- [cache writes are paid for and never read back](./cache-writes-with-no-reads/) — https://www.allanninal.dev/llm/cache-writes-with-no-reads/
- [a floating model alias silently changes model under you](./floating-alias-instead-of-pinned-snapshot/) — https://www.allanninal.dev/llm/floating-alias-instead-of-pinned-snapshot/
- [keys still work after their owner loses project access](./key-owner-lost-project-access/) — https://www.allanninal.dev/llm/key-owner-lost-project-access/
- [a model id in use is past its published shutdown date](./model-past-shutdown-date/) — https://www.allanninal.dev/llm/model-past-shutdown-date/
- [a model you still call retires in under 90 days](./model-retiring-within-90-days/) — https://www.allanninal.dev/llm/model-retiring-within-90-days/
- [no hard spend limit is set, so the bill has no ceiling](./no-organization-spend-limit/) — https://www.allanninal.dev/llm/no-organization-spend-limit/
- [output tokens, not input, are what the bill is made of](./output-tokens-dominate-cost/) — https://www.allanninal.dev/llm/output-tokens-dominate-cost/
- [prompt caching was never switched on anywhere](./prompt-caching-never-used/) — https://www.allanninal.dev/llm/prompt-caching-never-used/
- [429 credit_balance_exhausted retried forever as a rate limit](./quota-exhausted-not-rate-limited/) — https://www.allanninal.dev/llm/quota-exhausted-not-rate-limited/
- [reasoning tokens are billed as output but never returned](./reasoning-tokens-billed-invisibly/) — https://www.allanninal.dev/llm/reasoning-tokens-billed-invisibly/
- [a retired model id still sitting in the code](./retired-model-id-still-in-code/) — https://www.allanninal.dev/llm/retired-model-id-still-in-code/

## How to run one

Each folder holds the same script in Python and in Node.js, plus its test. Set the environment variables named in that folder's README and run it. Nothing writes, so there is no dry run to enable and no flag to be careful about — use a restricted, read-only credential and the worst case is that it tells you nothing is wrong.

## License

MIT. Use it, change it, ship it.
