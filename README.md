# LLM Proxy (Kilo Proxy & Aider Proxy)

This repository contains proxy servers designed to stabilize and optimize autonomous AI coding agents (such as Cline / Kilo Code and Aider) when running against local LLMs (e.g., `llama.cpp`).

## The Problem
When running agents against local LLMs on resource-constrained hardware (like APUs), two major issues occur:
1. **Prefill Collapse (KV Cache Destruction):** Agents send highly dynamic workspace file trees with every request. Even a 1-character change invalidates the KV cache, forcing a massive, slow full recompute (Prefill).
2. **Read Timeouts:** During these long prefill calculations, the HTTP connection goes completely silent. Clients (like VS Code or Aider) interpret this as a crashed server and drop the connection, leaving tasks unfinished.

## Features

- **Dynamic Prefix Masking:** Intercepts and replaces dynamic parts of the prompt (like timestamps and massive file trees) with static dummy text. This guarantees KV cache hits and drops the time-to-first-token to near zero on subsequent requests.
- **Heartbeat Streaming:** Sends empty SSE chunks every 60 seconds during long prefill times. This tricks the client into keeping the connection alive indefinitely.
- **Intelligent Compression:** Integrates with [Headroom](https://github.com/chopratejas/headroom) to smart-crush massive error logs and prevent VRAM Out-of-Memory (OOM) crashes.
- **Thought Pruning:** Detects endless "apology loops" or verbose reasoning from smaller models and actively truncates them (`[Reasoning Truncated by Kilo Proxy]`) to keep the context clean.
- **Format Normalization:** Catches formatting hallucinations (e.g., Markdown code blocks with incorrect language tags or malformed XML tool calls) and strictly normalizes them before they reach the agent parser.

## Usage
*This is an experimental tool heavily optimized for specific workflows. Adjust the rules and endpoints according to your local LLM setup.*

1. Point your agent's API base URL to the proxy (e.g., `http://localhost:5000/v1`)
2. The proxy will forward sanitized requests to your underlying `llama-server`.

## Related Article
The full architectural breakdown, development story, and detailed mechanisms of these proxies are documented in our tech blog article: [Link coming soon...]
