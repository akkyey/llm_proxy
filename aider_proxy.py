import json
import time
import queue
import threading
import logging
import re
import os
from flask import Flask, request, Response
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger("aider_proxy")

app = Flask(__name__)
LLAMA_SERVER_URL = "http://127.0.0.1:9090"
PORT = 9092

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
                            expected_fence = "```"
                            for msg in data.get('messages', []):
                                if msg.get('role') == 'system':
                                    sys_content = msg.get('content', '')
                                    # "path/to/filename.js" の直後にあるバッククォートを検出
                                    match = re.search(r'path/to/filename\.[a-zA-Z0-9]+\n(`{3,5})', sys_content)
                                    if match:
                                        expected_fence = match.group(1)
                                        logger.info(f"[FENCE DETECT] Aider expects fence: {expected_fence}")
                                        break

                            # レスポンス内のフェンス（バッククォート3〜5個、言語名オプション）をAiderの期待するフェンスに統一
                            # 例: ```python や ```` などをすべて Aiderが期待する expected_fence に置換
                            replaced_content = re.sub(r'^\s*`{3,5}[a-zA-Z0-9_-]*\s*$', expected_fence, content, flags=re.MULTILINE)
                            if replaced_content != content:
                                logger.info(f"[FILTER] Standardized fences to match Aider expectations ({expected_fence})")
                                content = replaced_content
                            
                            # リクエストとレスポンスを aider_logs に保存
                            log_dir = "/home/irom/dev/ollama/aider_logs"
                            os.makedirs(log_dir, exist_ok=True)
                            log_file = os.path.join(log_dir, f"chat_log_{int(time.time())}.json")
                            try:
                                with open(log_file, "w", encoding="utf-8") as f:
                                    json.dump({
                                        "timestamp": time.time(),
                                        "request": data,
                                        "original_response": msg.get('content', '') or '',
                                        "filtered_response": content
                                    }, f, ensure_ascii=False, indent=2)
                                logger.info(f"[LOG DUMP] Saved to {log_file}")
                            except Exception as le:
                                logger.error(f"[LOG DUMP ERROR] {le}")
                            
                            # Aiderのパーサーが正常に動作するよう、擬似ストリーミング（小分け送信）を行う
                            chunk_size = 40
                            delay = 0.01  # 秒
                            
                            for i in range(0, len(content), chunk_size):
                                chunk_text = content[i:i+chunk_size]
                                chunk_data = {
                                    "id": res.get("id", "chatcmpl-aider-proxy-resp"),
                                    "object": "chat.completion.chunk",
                                    "created": res.get("created", int(time.time())),
                                    "model": res.get("model", "qwen"),
                                    "choices": [{
                                        "index": 0,
                                        "delta": {
                                            "content": chunk_text
                                        },
                                        "finish_reason": None
                                    }]
                                }
                                yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
                                time.sleep(delay)
                            
                            # テキスト送信完了後、最後の空チャンクで finish_reason: "stop" (または元々の finish_reason) を送る
                            # これによりAiderが最後の閉じバッククォートをバッファで切り捨てるのを防ぐ
                            last_chunk = {
                                "id": res.get("id", "chatcmpl-aider-proxy-resp"),
                                "object": "chat.completion.chunk",
                                "created": res.get("created", int(time.time())),
                                "model": res.get("model", "qwen"),
                                "choices": [{
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": finish_reason
                                }]
                            }
                            if tool_calls:
                                last_chunk["choices"][0]["delta"]["tool_calls"] = tool_calls
                                
                            yield f"data: {json.dumps(last_chunk, ensure_ascii=False)}\n\n"
                            time.sleep(delay)
                        
                        yield "data: [DONE]\n\n"
                        logger.info(f"[SUCCESS] 処理完了 (所要時間: {time.time() - start_time:.1f}秒)")
                        break
                        
                    except queue.Empty:
                        # 待機中、60秒ごとに空のHeartbeatを送信して無通信によるタイムアウトを防ぐ
                        elapsed = time.time() - start_time
                        if time.time() - last_heartbeat >= 60:
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
    req_kwargs = {
        'method': request.method,
        'url': url,
        'headers': {k: v for k, v in request.headers.items() if k.lower() not in ['host', 'content-length']},
    }
    if request.is_json:
        req_kwargs['json'] = request.get_json(silent=True)
    elif request.data:
        req_kwargs['data'] = request.data
        
    try:
        resp = requests.request(**req_kwargs)
        return Response(resp.content, status=resp.status_code, headers=dict(resp.headers))
    except Exception as e:
        logger.error(f"[PROXY ERROR] {e}")
        return Response(json.dumps({"error": str(e)}), status=500, mimetype='application/json')

if __name__ == '__main__':
    logger.info(f"Starting Aider Proxy on port {PORT}...")
    logger.info(f"Forwarding to llama-server at: {LLAMA_SERVER_URL}")
    app.run(port=PORT, host='127.0.0.1')
