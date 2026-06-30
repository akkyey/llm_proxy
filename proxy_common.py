#!/usr/bin/env python3
"""
Aider Proxy 用の共通モジュール
"""

import time
import json
import logging
import re
import os
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

def send_dummy_chunks(content: str, res: Dict[str, Any], finish_reason: Optional[str] = None, tool_calls: Optional[List[Dict[str, Any]]] = None) -> None:
    """
    Aiderのパーサーが正常に動作するよう、擬似ストリーミング（小分け送信）を行う

    Args:
        content: 送信するコンテンツ
        res: レスポンス情報
        finish_reason: 終了理由
        tool_calls: ツール呼び出し情報
    """
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

def detect_expected_fence(data: Dict[str, Any]) -> str:
    """
    Aiderの期待するフェンス（バッククォートの数）を検出

    Args:
        data: リクエストデータ

    Returns:
        Aiderの期待するフェンス文字列
    """
    expected_fence = "```"
    for msg in data.get('messages', []):
        if msg.get('role') == 'system':
            sys_content = msg.get('content', '')
            # "path/to/filename.js" の直後にあるバッククォートを検出
            match = re.search(r'path/to/filename\\.[a-zA-Z0-9]+\\n(`{3,5})', sys_content)
            if match:
                expected_fence = match.group(1)
                logger.info(f"[FENCE DETECT] Aider expects fence: {expected_fence}")
                break
    return expected_fence

def standardize_fences(content: str, expected_fence: str) -> str:
    """
    レスポンス内のフェンス（バッククォート3〜5個、言語名オプション）をAiderの期待するフェンスに統一

    Args:
        content: レスポンス内容
        expected_fence: Aiderの期待するフェンス文字列

    Returns:
        統一された内容
    """
    # レスポンス内のフェンス（バッククォート3〜5個、言語名オプション）をAiderの期待するフェンスに統一
    # 例: ```python や ```` などをすべて Aiderが期待する expected_fence に置換
    replaced_content = re.sub(r'^\s*`{3,5}[a-zA-Z0-9_-]*\s*$', expected_fence, content, flags=re.MULTILINE)
    if replaced_content != content:
        logger.info(f"[FILTER] Standardized fences to match Aider expectations ({expected_fence})")
        content = replaced_content
    return content

def save_aider_log(data: Dict[str, Any], original_response: str, filtered_response: str) -> None:
    """
    Aiderのログをファイルに保存

    Args:
        data: リクエストデータ
        original_response: 元のレスポンス
        filtered_response: フィルタリング後のレスポンス
    """
    # リクエストとレスポンスを aider_logs に保存
    log_dir = "/home/irom/dev/ollama/aider_logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"chat_log_{int(time.time())}.json")
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": time.time(),
                "request": data,
                "original_response": original_response or '',
                "filtered_response": filtered_response
            }, f, ensure_ascii=False, indent=2)
        logger.info(f"[LOG DUMP] Saved to {log_file}")
    except Exception as le:
        logger.error(f"[LOG DUMP ERROR] {le}")

def passthrough_proxy(request: Any, url: str) -> Any:
    """
    チャット以外のリクエスト（/v1/modelsなど）をバックエンドへ透過転送する

    Args:
        request: Flaskのrequestオブジェクト
        url: 転送先URL

    Returns:
        FlaskのResponseオブジェクト
    """
    import requests
    from flask import Response
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
