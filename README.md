# LLM Proxy (Kilo Proxy & Aider Proxy)

[English](#english) | [日本語](#japanese)

---

<a id="english"></a>
## English

This repository contains two proxy servers designed to stabilize and optimize autonomous AI coding agents when running against local LLMs (e.g., `llama.cpp`).

### Included Proxies & Their Differences
Because each agent client interacts with the LLM differently, this repository includes two separate proxy scripts tailored to their specific quirks:

**1. `kilo_proxy.py` (For Cline / Kilo Code)**
- **Dynamic Prefix Masking:** Cline sends a massive workspace file tree and timestamps in every system prompt. This proxy forcefully masks these dynamic strings to protect the KV Cache, reducing subsequent prompt processing times to near zero.
- **Thought Pruning:** Automatically detects and strips out verbose "reasoning/apology loops" from smaller models to save context length and prevent AI degradation.
- **Tool Call Normalization:** Aggressively formats messy outputs (e.g., XML tags or Python-style syntax) into strict OpenAI-compatible `tool_calls`.

**2. `aider_proxy.py` (For Aider)**
- **Strict Markdown Parser Fix:** Local LLMs often prepend language tags to code blocks (e.g., ```python). Aider's "Whole" parser crashes on this. The proxy actively strips these tags on the fly.
- **"95% Freeze" Prevention:** Aider sometimes drops the connection if `finish_reason: stop` is sent in the same chunk as the final backticks. This proxy intentionally delays and separates the `finish_reason` into an empty chunk to ensure 100% successful code application.

### The Core Problems Solved
When running agents against local LLMs on resource-constrained hardware (like APUs), two major issues occur:
1. **Prefill Collapse:** Even a 1-character change in the prompt invalidates the KV cache, forcing a massive, slow full recompute (Prefill).
2. **Read Timeouts:** During long prefill calculations, the HTTP connection goes completely silent. Clients interpret this as a crashed server and drop the connection.

Both proxies utilize **Heartbeat Streaming** (sending empty SSE chunks every 60 seconds) to trick clients into waiting indefinitely. Additionally, `kilo_proxy.py` integrates with [Headroom](https://github.com/chopratejas/headroom) for intelligent context compression to prevent Out-of-Memory (OOM) errors (Aider handles its own context summarization natively).

### Usage
*This is an experimental tool heavily optimized for specific workflows. Adjust the rules and endpoints according to your local LLM setup.*
1. Run the proxy script suitable for your agent.
2. Point your agent's API base URL to the proxy (e.g., `http://localhost:5000/v1`).
3. The proxy will forward sanitized requests to your underlying `llama-server`.

### Related Article
The full architectural breakdown, development story, and detailed mechanisms of these proxies are documented in our tech blog article (Japanese): [Zenn Article](https://zenn.dev/akkyey/articles/abf10f9eb05f6a)

---

<a id="japanese"></a>
## 日本語

本リポジトリは、ローカルLLM（`llama.cpp`等）環境で自律型AIコーディングエージェントを安定して稼働させるために開発されたプロキシサーバー群です。

### 2つのプロキシとその違い
エージェントごとにLLMとの通信手法やパーサーの癖が異なるため、本リポジトリではそれぞれの仕様に特化した2つのプロキシを用意しています。

**1. `kilo_proxy.py` (Cline / Kilo Code 用)**
- **動的プレフィックスのマスク:** Clineは毎回の通信で「現在時刻」や「巨大なファイルツリー」を送信します。これをダミー文字に強制置換することでKVキャッシュを保護し、2回目以降の推論を超高速化します。
- **推論の枝刈り (Thought Pruning):** 小型モデル特有の「長々とした言い訳ループ」を検知し、`[Reasoning Truncated]` と強制置換してコンテキストのノイズを排除します。
- **ツールコール矯正:** LLMが誤って出力したXMLタグや構文エラーを、正規の `tool_calls` フォーマットに力技で補正します。

**2. `aider_proxy.py` (Aider 用)**
- **マークダウンタグの除去:** ローカルLLMはコードブロックに ```python のように言語名を付けがちですが、AiderのWholeパーサーはこれを許容しません。プロキシ側でこの言語名を正規表現で削ぎ落とします。
- **「95%フリーズ問題」の回避:** Aiderは最後のテキストチャンクと同時に `finish_reason: stop` を受信すると、末尾数文字のバッファを切り捨ててパースに失敗することがあります。これを防ぐため、テキストを送り切った後に完全に独立した空チャンクで `finish_reason` のみを送信します。

### 共通で解決する課題
限られたリソース（APUなど）のローカル環境でエージェントを動かすと、以下の問題が発生します：
1. **プレフィル崩壊:** プロンプトが1文字でも変化するとKVキャッシュが破棄され、数十分もの長いプレフィル（再計算）が発生してしまいます。
2. **タイムアウト切断:** 長いプレフィル計算中の沈黙を、クライアント側が「サーバーハング」と誤検知して接続を切断してしまいます。

両プロキシとも、計算中に60秒間隔で空のチャンクを送信する **ハートビート ストリーミング** を実装してタイムアウトを防ぎます。さらに `kilo_proxy.py` では、OSSの [Headroom](https://github.com/chopratejas/headroom) と連携してエラーログなどをインテリジェントに圧縮し、VRAM溢れ（OOM）を未然に防いでいます（Aider環境ではAider自身の自動要約機能にコンテキスト管理を委ねています）。

### 使い方
*※特定の環境に特化して最適化された実験的なツールです。ご自身のローカルLLM環境に合わせて調整してご使用ください。*
1. 使用するエージェントに合わせたプロキシスクリプトを起動します。
2. エージェントの API Base URL をプロキシ（例: `http://localhost:5000/v1`）に向けます。
3. プロキシがリクエストを無菌化し、バックエンドの `llama-server` へ転送します。

### 関連記事
これらのプロキシのアーキテクチャや開発の裏話については、技術ブログにて詳細に解説しています： [Zenn 記事](https://zenn.dev/akkyey/articles/abf10f9eb05f6a)
