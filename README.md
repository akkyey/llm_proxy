# LLM Proxy (Kilo Proxy & Aider Proxy)

[English](#english) | [日本語](#japanese)

---

<a id="english"></a>
## English

This repository contains proxy servers designed to stabilize and optimize autonomous AI coding agents (such as Cline / Kilo Code and Aider) when running against local LLMs (e.g., `llama.cpp`).

### The Problem
When running agents against local LLMs on resource-constrained hardware (like APUs), two major issues occur:
1. **Prefill Collapse (KV Cache Destruction):** Agents send highly dynamic workspace file trees with every request. Even a 1-character change invalidates the KV cache, forcing a massive, slow full recompute (Prefill).
2. **Read Timeouts:** During these long prefill calculations, the HTTP connection goes completely silent. Clients (like VS Code or Aider) interpret this as a crashed server and drop the connection, leaving tasks unfinished.

### Features
- **Dynamic Prefix Masking:** Intercepts and replaces dynamic parts of the prompt (like timestamps and massive file trees) with static dummy text. This guarantees KV cache hits and drops the time-to-first-token to near zero on subsequent requests.
- **Heartbeat Streaming:** Sends empty SSE chunks every 60 seconds during long prefill times. This tricks the client into keeping the connection alive indefinitely.
- **Intelligent Compression:** Integrates with [Headroom](https://github.com/chopratejas/headroom) to smart-crush massive error logs and prevent VRAM Out-of-Memory (OOM) crashes.
- **Thought Pruning:** Detects endless "apology loops" or verbose reasoning from smaller models and actively truncates them (`[Reasoning Truncated by Kilo Proxy]`) to keep the context clean.
- **Format Normalization:** Catches formatting hallucinations (e.g., Markdown code blocks with incorrect language tags or malformed XML tool calls) and strictly normalizes them before they reach the agent parser.

### Usage
*This is an experimental tool heavily optimized for specific workflows. Adjust the rules and endpoints according to your local LLM setup.*
1. Point your agent's API base URL to the proxy (e.g., `http://localhost:5000/v1`).
2. The proxy will forward sanitized requests to your underlying `llama-server`.

### Related Article
The full architectural breakdown, development story, and detailed mechanisms of these proxies are documented in our tech blog article: [Link coming soon...]

---

<a id="japanese"></a>
## 日本語

本リポジトリは、ローカルLLM（`llama.cpp`等）環境で自律型AIコーディングエージェント（Cline / Kilo Code、Aiderなど）を安定して稼働させるために開発されたプロキシサーバー群です。

### 解決する課題
限られたリソース（APUなど）のローカル環境でエージェントを動かすと、主に2つの大きな問題が発生します：
1. **Prefill崩壊（KVキャッシュの破棄）:** エージェントは毎回の通信で「変動する巨大なファイルツリー」を送信します。1文字でも変化するとKVキャッシュが破棄され、数十分もの長いPrefill（再計算）が発生してしまいます。
2. **タイムアウト切断:** 長いPrefill計算中、HTTP通信は完全に沈黙します。VS CodeやAider側はこれを「サーバーハング」と誤検知し、強制的に接続を切断してしまいます。

### 主な機能
- **動的Prefixのマスク (KVキャッシュ保護):** プロンプト内の動的な文字列（現在時刻やファイルツリー）を傍受し、ダミー文字列に強制置換します。これによりキャッシュヒット率を劇的に高め、2回目以降の推論を超高速化します。
- **Heartbeat ストリーミング:** 長い計算中に60秒間隔で空のチャンク（Heartbeat）を送信し、クライアント側のタイムアウト切断を防止します。
- **インテリジェント圧縮:** OSSの[Headroom](https://github.com/chopratejas/headroom)と統合し、不要なファイル読み込み履歴やエラーログを圧縮してVRAM溢れ（OOM）を防ぎます。
- **推論の枝刈り (Thought Pruning):** 小型モデル特有の「長々とした言い訳」や無限ループを検知し、`[Reasoning Truncated by Kilo Proxy]` と強制置換してノイズを排除します。
- **フォーマット矯正:** ローカルLLMが起こしやすい出力フォーマットの揺れ（不正なXMLタグやバッククォートの付与ミス）を検知し、エージェントがパースできる正しい形式に補正します。

### 使い方
*※特定の環境に特化して最適化された実験的なツールです。ご自身のローカルLLM環境に合わせて調整してご使用ください。*
1. エージェントの API Base URL をプロキシ（例: `http://localhost:5000/v1`）に向けます。
2. プロキシがリクエストを無菌化し、バックエンドの `llama-server` へ転送します。

### 関連記事
これらのプロキシのアーキテクチャや開発の裏話については、技術ブログにて詳細に解説しています： [近日公開予定...]
