import json
import time
import queue
import threading
import logging
import re
import os
from flask import Flask, request, Response
import requests
from typing import Dict, Any, Optional

# 共通モジュールのインポート
import proxy_common

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger("aider_proxy")

app = Flask(__name__)
LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "http://127.0.0.1:9090")
PORT = int(os.environ.get("AIDER_PROXY_PORT", 9092))
LOG_DIR = os.environ.get("AIDER_PROXY_LOG_DIR", "/home/irom/dev/ollama/aider_logs")
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("HEARTBEAT_INTERVAL_SEC", 60))

@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy(path):
    # 余分な空白を除去
    clean_path = path.replace(' ', '')
    url = f"{LLAMA_SERVER_URL}/{clean_path}"

    # チャット生成リクエストの場合のみ、タイムアウト防止（Heartbeat送信）処理を行う
    if request.method == 'POST' and clean_path.endswith('chat/completions'):
        data = request.get_json(silent=True) or {}
        is_stream = data.get('stream', False)

        # Aiderのタイムアウトを防ぐため、バックエンド(llama-server)には非ストリーミング(stream=False)でリクエストし、
        # プロキシ側でダミーのHeartbeatを送りながらバックエンドの生成完了を待つ。
        data['stream'] = False

        headers = {k: v for k, v in request.headers.items() if k.lower() not in ['host', 'content-length']}

        logger.info(f"[REQUEST] path={clean_path} stream={is_stream} prompt_len={len(str(data.get('messages', '')))}")

        if is_stream:
            def generate():
                # クライアントの初期接続タイムアウトを防ぐために、即座に空のダミーチャンクを返す
                dummy = {
                    "id": "chatcmpl-aider-proxy-init",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "qwen",
                    "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]
                }
                yield f'data: {json.dumps(dummy)}\n\n'

                q = queue.Queue()

                # バックエンドとの通信を別スレッドで非同期に実行 (タイムアウトは1時間)
                def worker():
                    try:
                        resp = requests.post(url, json=data, headers=headers, timeout=3600)
                        resp.raise_for_status()
                        q.put(resp.json())
                    except Exception as e:
                        logger.error(f"[WORKER ERROR] {e}")
                        q.put(e)

                threading.Thread(target=worker, daemon=True).start()

                start_time = time.time()
                last_heartbeat = time.time()

                while True:
                    try:
                        # 1秒ごとにキューを確認
                        res = q.get(timeout=1.0)

                        if isinstance(res, Exception):
                            # バックエンドでエラーが起きた場合、エラーログをクライアントに返す
                            err_chunk = {
                                "id": "chatcmpl-aider-proxy-error",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "choices": [{"index": 0, "delta": {"content": f"\n[Aider Proxy Backend Error: {res}]\n"}, "finish_reason": "stop"}]
                            }
                            yield f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            break

                        # 正常に応答が返ってきた場合、そのテキストをストリーミングとしてクライアントに流す
                        choices = res.get('choices', [])
                        if choices:
                            msg = choices[0].get('message', {})
                            content = msg.get('content', '') or ''
                            tool_calls = msg.get('tool_calls')
                            finish_reason = choices[0].get('finish_reason', 'stop')

                            # Aiderが期待するフェンスの長さを検出
                            expected_fence = proxy_common.detect_expected_fence(data)

                            # レスポンス内のフェンスをAiderの期待するフェンスに統一
                            content = proxy_common.standardize_fences(content, expected_fence)

                            # ログ保存
                            proxy_common.save_aider_log(data, msg.get('content', '') or '', content)

                            # 擬似ストリーミング
                            yield from proxy_common.send_dummy_chunks(content, res, finish_reason, tool_calls)

                        yield "data: [DONE]\n\n"
                        logger.info(f"[SUCCESS] 処理完了 (所要時間: {time.time() - start_time:.1f}秒)")
                        break

                    except queue.Empty:
                        # 待機中、60秒ごとに空のHeartbeatを送信して無通信によるタイムアウトを防ぐ
                        elapsed = time.time() - start_time
                        if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                            heartbeat = {
                                "id": "chatcmpl-aider-proxy-heartbeat",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]
                            }
                            yield f"data: {json.dumps(heartbeat)}\n\n"
                            last_heartbeat = time.time()
                            logger.info(f"[HEARTBEAT] 待機中... ({elapsed:.0f}秒経過)")

            return Response(generate(), mimetype='text/event-stream')

        else:
            # 非ストリーミングモードの場合
            try:
                resp = requests.post(url, json=data, headers=headers, timeout=3600)
                return Response(resp.content, status=resp.status_code, headers=dict(resp.headers))
            except Exception as e:
                logger.error(f"[NON-STREAM ERROR] {e}")
                return Response(json.dumps({"error": str(e)}), status=500, mimetype='application/json')

    # チャット以外のリクエスト（/v1/modelsなど）はそのままバックエンドへ転送
    return proxy_common.passthrough_proxy(request, url)

if __name__ == '__main__':
    logger.info(f"Starting Aider Proxy on port {PORT}...")
    logger.info(f"Forwarding to llama-server at: {LLAMA_SERVER_URL}")
    app.run(port=PORT, host='127.0.0.1')
