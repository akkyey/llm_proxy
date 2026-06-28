import ast
import json
import os
import re
import logging
import queue
import threading
import time
import uuid
import subprocess
from datetime import datetime

from flask import Flask, request, Response
import requests
from headroom import compress

# --- ペイロード観測用ログ設定 ---
# root実行時でも迷子にならないよう、Linuxの標準的な共有ログ領域、または環境変数から取得
PAYLOAD_LOG_DIR = os.getenv("KILO_PROXY_LOG_DIR", "/var/log/kilo_proxy")
os.makedirs(PAYLOAD_LOG_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger("kilo_proxy")

# --- 定数定義 ---
# Kilo Code (Cline) のタイムアウト制約に対処するための強制システム命令。
# LLMに環境の制約を教え込み、長文出力による暴走を防止する。
SYSTEM_DIRECTIVE = (
    "\n\n[CRITICAL SYSTEM DIRECTIVE]\n"
    "1. 思考プロセス（独り言）の出力は最小限に抑え、素早く結果を出力してください。\n"
    "2. 既存のファイルを修正する際は、ソースコード全体を再出力しないでください。変更箇所（差分）のみを出力するか、適切なエディタの検索・置換フォーマットを使用してください。\n"
    "3. 必要なコードや成果物は、ただチャットに表示するだけでなく、エディタツールを使って直接ファイルに書き出してください。\n"
    "4. 複数のツールを連続して実行することが許可されています。自律的に判断し、タスクが完了するまでツールを呼び出し続けてください。\n"
    "5. All your responses and thoughts MUST be in Japanese. (必ず日本語で思考・返答してください。)\n"
    "6. タスクの区切りやエラー発生時には、万が一のシステムクラッシュに備え、必ず `.kilo/status.md` に現在の状況と次のタスクを上書き保存（オートセーブ）してください。"
)

# ストリーミング時のHeartbeat送信間隔（秒）
# Kilo Codeの接続タイムアウト（約5分）を防ぐため、定期的に空チャンクを送信する
HEARTBEAT_INTERVAL_SEC = 60

# フェールセーフ切断までの最大待機時間（秒）
# VS Code自体の根深いタイムアウト（約5分）を回避するための安全マージン
FAILSAFE_TIMEOUT_SEC = 270


def dump_payload(data):
    """Kilo Codeからの生リクエストペイロードをJSONファイルにダンプする。
    KVキャッシュ破壊の原因となる動的変数の位置を特定するための観測機能。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filepath = os.path.join(PAYLOAD_LOG_DIR, f"payload_{ts}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # messagesの概要をコンソールにも出力
    messages = data.get("messages", [])
    logger.info(f"[DUMP] model={data.get('model','?')} messages={len(messages)} -> {filepath}")
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "") or ""
        logger.info(f"  [{i}] role={role} len={len(content)} first100={content[:100]!r}")


def mask_dynamic_prefix(text):
    """環境変数ブロック内の動的文字列（現在時刻、ワークスペースのパスなど）を固定ダミーに置換する。
    これにより、毎回の通信で微妙に変化するPrefixを完全に固定し、KVキャッシュの崩壊を防ぐ。"""
    # System Message 内の Today's date のマスク（これが0時にキャッシュを破壊する元凶）
    text = re.sub(r"Today's date: [^\n]+", "Today's date: [MASKED BY PROXY]", text)
    # Current time のマスク
    text = re.sub(r'Current time: [^\n]+', 'Current time: [MASKED BY PROXY]', text)
    # Working directory のマスク
    text = re.sub(r'Working directory: [^\n]+', 'Working directory: [MASKED BY PROXY]', text)
    return text


def truncate_workspace(text):
    """KVキャッシュ保護のため、ワークスペースファイルツリーを固定長ダミーに置換する。

    llama.cppのKVキャッシュはPrefix一致（先頭からの完全一致）でしか再利用できないため、
    会話中にファイルの増減で変化するワークスペースツリーがプロンプト中腹にあると、
    それ以降のキャッシュが全破棄（Full Recompute）される。
    固定文字列に置換することでPrefixの一致条件を維持し、2回目以降のPrefillを消滅させる。"""
    start_marker = "# Current Workspace Directory"
    end_marker1 = "</environment_details>"
    end_marker2 = "You have not created a todo list yet"

    start_idx = text.find(start_marker)
    if start_idx != -1:
        end_idx = text.find(end_marker2, start_idx)
        if end_idx == -1:
            end_idx = text.find(end_marker1, start_idx)

        if end_idx != -1:
            return (
                text[:start_idx]
                + start_marker
                + " (Workspace file list truncated by Agentic Proxy for KV cache protection."
                + " Use `list_files` tool if needed.)\n\n"
                + text[end_idx:]
            )
    return text


def extract_python_call(content):
    # python形式の関数呼び出しをASTで安全にパース
    for match in re.finditer(r'\b([a-zA-Z0-9_]+)\s*\(([\s\S]*?)\)', content):
        func_str = match.group(0)
        try:
            tree = ast.parse(func_str)
            if isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Call):
                call = tree.body[0].value
                if isinstance(call.func, ast.Name):
                    tool_name = call.func.id
                    args = {}
                    for kw in call.keywords:
                        if getattr(kw.value, 'value', None) is not None:
                            args[kw.arg] = kw.value.value
                        elif getattr(kw.value, 's', None) is not None:
                            args[kw.arg] = kw.value.s
                    if args:
                        return tool_name, args, match.start()
        except Exception:
            continue
    return None

def get_max_context_chars():
    """llama-serverの起動オプションから -c の値を取得し、安全な最大文字数（90%判定用）を算出する"""
    try:
        output = subprocess.check_output(["ps", "aux"]).decode()
        match = re.search(r'llama-server.*?-c\s+(\d+)', output)
        if match:
            tokens = int(match.group(1))
            # 1トークン ≈ 3文字 として、最大許容文字数をざっくり算出（余裕を持って2.8倍）
            return int(tokens * 2.8)
    except Exception:
        pass
    # デフォルトのフェイルセーフ (65536 * 2.8 ≈ 183500)
    return 183500


def fix_tool_calls(resp_data):
    """
    ローカルLLM特有のフォーマット揺れ（XMLタグやMarkdownコードブロック、Python形式）を
    OpenAI互換の `tool_calls` 形式に補正する。
    """
    choices = resp_data.get('choices', [])
    if not choices:
        return resp_data

    msg = choices[0].get('message', {})
    content = msg.get('content', '')

    if not isinstance(content, str):
        return resp_data

    # すでに正常なツールコールがあれば何もしない
    if msg.get('tool_calls'):
        return resp_data

    # パターン1: XMLタグ形式 (<tool_name>{JSON}</tool_name>)
    xml_match = re.search(r'<([a-zA-Z0-9_]+)>\s*(\{.*?\})\s*</\1>', content, re.DOTALL)

    if xml_match:
        tool_args_str = xml_match.group(2)
        try:
            parsed = json.loads(tool_args_str)  # JSONとして有効か検証
            actual_name = parsed.get("name", xml_match.group(1))
            actual_args = parsed.get("arguments", parsed)
            
            msg['content'] = content[:xml_match.start()].strip() or None
            msg['tool_calls'] = [{
                'id': f'call_proxy_{uuid.uuid4().hex[:12]}',
                'type': 'function',
                'function': {
                    'name': actual_name,
                    'arguments': json.dumps(actual_args, ensure_ascii=False) if isinstance(actual_args, dict) else actual_args
                }
            }]
            resp_data['choices'][0]['finish_reason'] = 'tool_calls'
            return resp_data
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"XMLツールコールのJSONパース失敗: {e}")

    # パターン2: 生JSON形式 ({"name": ..., "arguments": ...})
    # LLMがフォーマットしてインデントを入れた場合に対応するため正規表現で検索
    json_match = re.search(r'\{\s*"name"\s*:', content)
    if json_match:
        json_start = json_match.start()
        json_str = content[json_start:].strip()

        parsed_tool = None
        while len(json_str) > 10:
            try:
                parsed_tool = json.loads(json_str)
                break
            except (json.JSONDecodeError, ValueError):
                json_str = json_str[:-1]

        if parsed_tool and 'name' in parsed_tool and 'arguments' in parsed_tool:
            msg['content'] = content[:json_start].strip() or None
            msg['tool_calls'] = [{
                'id': f'call_proxy_{uuid.uuid4().hex[:12]}',
                'type': 'function',
                'function': {
                    'name': parsed_tool['name'],
                    'arguments': json.dumps(parsed_tool['arguments'], ensure_ascii=False) if isinstance(parsed_tool['arguments'], dict) else parsed_tool['arguments']
                }
            }]
            resp_data['choices'][0]['finish_reason'] = 'tool_calls'
            return resp_data

    # パターン3: Markdown Bash/Sh ブロック (```bash ... ```)
    # LLMがツール呼び出しフォーマットを忘れて生のスクリプトを出力した場合の究極のフォールバック
    bash_match = re.search(r'```(?:bash|sh)\n(.*?)```', content, re.DOTALL)
    if bash_match:
        script = bash_match.group(1).strip()
        if script:
            msg['content'] = content[:bash_match.start()].strip() or None
            msg['tool_calls'] = [{
                'id': f'call_proxy_{uuid.uuid4().hex[:12]}',
                'type': 'function',
                'function': {
                    'name': 'bash',
                    'arguments': json.dumps({"command": script}, ensure_ascii=False)
                }
            }]
            resp_data['choices'][0]['finish_reason'] = 'tool_calls'
            logger.info(f"[FALLBACK] Markdown bashブロックをツールコールに変換: {script[:30]}...")
            return resp_data

    return resp_data


def remove_unanchored_patterns(obj):
    """JSON Schema内のアンカーなし正規表現パターンを除去する。

    ローカルLLM（特にllama.cpp）は、JSON Schemaのpatternフィールドに含まれる
    アンカーなし正規表現（^...$で囲まれていないもの）を正しく処理できず、
    不正な出力やパースエラーの原因となる。
    アンカー付きパターン（^...$）のみを残すことで、ツールコールの成功率を向上させる。"""
    if isinstance(obj, dict):
        if 'pattern' in obj:
            p = obj['pattern']
            if isinstance(p, str) and not (p.startswith('^') and p.endswith('$')):
                del obj['pattern']
        for k, v in list(obj.items()):
            remove_unanchored_patterns(v)
    elif isinstance(obj, list):
        for item in obj:
            remove_unanchored_patterns(item)


# --- キャッシュ分析用グローバル状態とヘルパー関数 ---
LAST_REQUEST_TEXT = ""
LAST_SYSTEM_PROMPT = ""
LAST_REQUEST_LOCK = threading.Lock()

def serialize_request_for_caching(data):
    """リクエストデータから、LLMへの入力プロンプトに近い平坦なテキスト表現を作成する。
    LCP（最長共通プレフィックス）算出のベースとして使用。"""
    parts = []
    # 1. ツール定義
    if 'tools' in data:
        parts.append(f"[TOOLS]\n{json.dumps(data['tools'], sort_keys=True)}")
    
    # 2. メッセージ履歴
    parts.append("[MESSAGES]")
    for msg in data.get('messages', []):
        role = msg.get('role', '')
        content = msg.get('content', '')
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if part.get('type') == 'text':
                    text_parts.append(part.get('text', ''))
            content_str = "\n".join(text_parts)
        else:
            content_str = str(content)
        parts.append(f"<{role}>\n{content_str}\n</{role}>")
    
    return "\n".join(parts)


def get_lcp_and_breakpoint(s1, s2):
    """2つの文字列 s1 と s2 の最長共通プレフィックスの長さと、不一致が発生した直後の部分（ブレークポイント）を取得する。"""
    min_len = min(len(s1), len(s2))
    lcp_len = 0
    for i in range(min_len):
        if s1[i] != s2[i]:
            break
        lcp_len += 1
    
    if lcp_len == 0:
        return 0, "No common prefix."
    
    # 不一致箇所の周辺コンテキストを抽出
    start = max(0, lcp_len - 15)
    end = min(len(s2), lcp_len + 35)
    breakpoint_context = s2[start:end]
    marker_idx = lcp_len - start
    marked_context = (
        breakpoint_context[:marker_idx] 
        + " 💥[BREAKPOINT]💥 " 
        + breakpoint_context[marker_idx:]
    )
    marked_context = marked_context.replace('\n', '\\n')
    return lcp_len, marked_context


# --- Flaskアプリケーション ---
app = Flask(__name__)


@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
def proxy(path):
    # 余分な空白が混入した場合（%20）に備えて除去
    clean_path = path.replace(' ', '')
    url = f'http://127.0.0.1:9090/{clean_path}'

    if request.method == 'POST' and clean_path.endswith('chat/completions'):
        # --- [B-1] 安全なJSONパース ---
        data = request.get_json(silent=True)
        if data is None:
            return Response(
                '{"error": "Invalid JSON payload"}',
                status=400, mimetype='application/json'
            )

        dump_payload(data)  # 観測: Kilo Codeの生ペイロードをダンプ

        # --- [キャッシュ崩壊リアルタイム分析] ---
        global LAST_REQUEST_TEXT, LAST_SYSTEM_PROMPT
        current_req_text = serialize_request_for_caching(data)
        current_sys_msg = next((m.get('content', '') for m in data.get('messages', []) if m.get('role') == 'system'), "")

        with LAST_REQUEST_LOCK:
            if LAST_REQUEST_TEXT:
                lcp_len, breakpoint_ctx = get_lcp_and_breakpoint(LAST_REQUEST_TEXT, current_req_text)
                ratio = (lcp_len / len(current_req_text)) * 100 if len(current_req_text) > 0 else 0
                logger.info(
                    f"[CACHE_ANALYTICS] 総文字数: {len(current_req_text)} | "
                    f"一致文字数: {lcp_len} ({ratio:.1f}%) | "
                    f"分岐点: {breakpoint_ctx}"
                )
            else:
                logger.info(f"[CACHE_ANALYTICS] 初回リクエスト (総文字数: {len(current_req_text)})")
                ratio = 100.0
            LAST_REQUEST_TEXT = current_req_text
            LAST_SYSTEM_PROMPT = current_sys_msg

        # --- [AUTO-RESET SAFEGUARD] ---
        # 巨大な再計算（Full Recompute）によるOOM死を防ぐため、
        # キャッシュのプレフィックス（LCP）が過去のリクエストより大幅に後退した場合、
        # プロキシがリクエストを遮断し、VS Codeへ直接エラー（警告）を返す。
        invalidated_old_chars = len(LAST_REQUEST_TEXT) - lcp_len if ('lcp_len' in locals() and LAST_REQUEST_TEXT) else 0

        safeguard_msg = None
        if LAST_SYSTEM_PROMPT and current_sys_msg != LAST_SYSTEM_PROMPT:
            safeguard_msg = (
                "⚠️ **【ルール変更によるキャッシュ崩壊を検知】** ⚠️\n\n"
                "`KILO_RULES.md` などのシステムプロンプトがチャットの途中で書き換えられました。\n"
                "これにより過去の記憶（キャッシュ）がすべて破棄され、深刻な再計算が発生するため処理を遮断しました。\n\n"
                "**【アクション】**\n"
                "新しいルールを安全に適用するため、**新規チャット（New Chat）**を開き、「作業を再開して」と指示してください。"
            )
        elif len(current_req_text) > 40000 and invalidated_old_chars > 15000:
            safeguard_msg = (
                "⚠️ **【Kilo Proxy 安全装置が発動しました】** ⚠️\n\n"
                "文脈の忘却（Window Sliding等）に伴う巨大なキャッシュ崩壊を検知しました。\n"
                "このまま進めるとサーバーがクラッシュするため処理を遮断しました。\n\n"
                "**【アクション】**\n"
                "現在の状況と次のタスクを `.kilo/status.md` に手動で保存し、**新規チャット（New Chat）**を開始してください。"
            )

        if safeguard_msg:
            logger.warning("[SAFEGUARD] 巨大なキャッシュ崩壊を検知。リクエストを遮断し、クライアントに警告を返します。")
            if data.get('stream', False):
                def safeguard_generate():
                    chunk = {
                        "id": "chatcmpl-safeguard",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "proxy",
                        "choices": [{"index": 0, "delta": {"content": safeguard_msg}, "finish_reason": "stop"}]
                    }
                    yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'
                    yield 'data: [DONE]\n\n'
                return Response(safeguard_generate(), mimetype='text/event-stream')
            else:
                return Response(
                    json.dumps({
                        "id": "chatcmpl-safeguard",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "proxy",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": safeguard_msg}, "finish_reason": "stop"}]
                    }, ensure_ascii=False),
                    mimetype='application/json'
                )

        # --- [Context Architecture最適化] ---
        # Kilo Codeが毎ターン送信してくる巨大なファイルツリー（3万トークン）を削減し、
        # llama.cppのKVキャッシュ破壊とプレフィル地獄を防ぐ。
        messages = data.get('messages', [])

        # [C-4] System Directive の注入（定数から参照）と System Message のマスク処理
        for msg in messages:
            if msg.get('role') == 'system':
                msg['content'] = mask_dynamic_prefix(msg.get('content', ''))
                msg['content'] += SYSTEM_DIRECTIVE

        # --- [NEW] Attention Anchor (Lost in the middle 対策) ---
        # Kilo Codeがシステムプロンプトの上部に挿入する「カスタムプロンプト（モード上書き）」を抽出し、
        # ユーザーメッセージの最後に強制的に再注入することで、14Bモデルの忘却を完全に防ぐ。
        sys_msg = next((m for m in messages if m.get('role') == 'system'), None)
        usr_msg = next((m for m in reversed(messages) if m.get('role') == 'user'), None)
        if sys_msg and usr_msg:
            parts = sys_msg['content'].split('You are powered by the model named')
            if len(parts) > 1:
                preceding_text = parts[0]
                last_bullet_idx = preceding_text.rfind('\n  - ')
                if last_bullet_idx != -1:
                    end_of_bullet_line = preceding_text.find('\n', last_bullet_idx + 1)
                    if end_of_bullet_line != -1:
                        custom_override = preceding_text[end_of_bullet_line:].strip()
                        if custom_override and len(custom_override) > 5:
                            injection = f"\n\n<CRITICAL_INSTRUCTION_REMINDER>\n{custom_override}\n</CRITICAL_INSTRUCTION_REMINDER>"
                            if isinstance(usr_msg['content'], list):
                                usr_msg['content'].append({"type": "text", "text": injection})
                            elif isinstance(usr_msg['content'], str):
                                usr_msg['content'] += injection
                            logger.info("[ATTENTION ANCHOR] カスタムプロンプトを抽出してユーザーメッセージ末尾に注入しました。")

        # --- [NEW] コンテキスト肥大化の90%限界検知による自動終了指示 ---
        # ユーザー指示: コンテキストサイズが変わるかもしれないので、起動プロセスのコンテキストサイズ（トークン）から
        # 90%のしきい値を動的に計算し、超えたらユーザーメッセージに緊急終了指示を注入する。
        try:
            current_total_chars = sum(len(str(m.get('content', ''))) for m in data.get('messages', []))
            max_chars = get_max_context_chars()
            if current_total_chars > max_chars * 0.90:
                warning_injection = (
                    "\n\n[CRITICAL EMERGENCY DIRECTIVE]\n"
                    "🚨 コンテキストサイズが限界容量の90%を超過しました！ 🚨\n"
                    "これ以上作業を続けるとシステムがクラッシュします。\n"
                    "現在実行中のタスクを直ちに中断し、これまでのすべての作業状況と次の課題を `.kilo/status.md` に保存してください。\n"
                    "保存後、必ずユーザーに「コンテキスト限界のため保存しました。New Chatをお願いします」と報告し、絶対に他のツールを実行しないでください。"
                )
                if usr_msg:
                    if isinstance(usr_msg['content'], list):
                        usr_msg['content'].append({"type": "text", "text": warning_injection})
                    elif isinstance(usr_msg['content'], str):
                        usr_msg['content'] += warning_injection
                    logger.warning(f"[CONTEXT LIMIT] {current_total_chars}/{max_chars} (90%超過) を検知。緊急終了指示をユーザーメッセージに注入しました。")
        except Exception as e:
            logger.error(f"コンテキスト限界検知エラー: {e}")

        # --- [NEW] Assistantの推論（Thought）枝刈りによるコンテキスト保護 ---
        # 過去のAssistantの発言（独り言）を削除し、純粋な「行動履歴（Tool Calls）」だけを残す
        for msg in messages:
            if msg.get('role') == 'assistant':
                content = msg.get('content')
                if isinstance(content, str) and len(content) > 100 and msg.get('tool_calls'):
                    msg['content'] = "[Reasoning Truncated by Kilo Proxy]"

        # --- [NEW] Headroomによるコンテキスト圧縮 ---
        original_len = sum(len(str(m.get('content', ''))) for m in messages)
        try:
            # Headroomに圧縮を委譲 (CompressResultが返る)
            comp_res = compress(messages, model=data.get('model', 'claude-3-5-sonnet-20241022'))
            if hasattr(comp_res, 'messages'):
                messages = comp_res.messages
                data['messages'] = messages
        except Exception as e:
            logger.error(f"[HEADROOM] 圧縮エラー: {e}")
        
        compressed_len = sum(len(str(m.get('content', ''))) for m in messages)
        if original_len > 0:
            reduction = 100 - (compressed_len / original_len * 100)
            logger.info(f"[HEADROOM] 圧縮完了: {original_len}文字 -> {compressed_len}文字 (圧縮率: {reduction:.1f}%削減)")
        else:
            logger.info("[HEADROOM] メッセージが空のためスキップしました。")

        # [C-2] ワークスペースツリーの切り詰め（KVキャッシュ保護）
        for msg in messages:
            if msg.get('role') == 'user':
                content = msg.get('content')
                if isinstance(content, list):
                    for part in content:
                        if part.get('type') == 'text' and '<environment_details>' in part.get('text', ''):
                            part['text'] = mask_dynamic_prefix(truncate_workspace(part['text']))
                elif isinstance(content, str):
                    if '<environment_details>' in content:
                        msg['content'] = mask_dynamic_prefix(truncate_workspace(content))

        is_stream = data.get('stream', False)

        if 'tools' in data:
            # --- [MCP Memory Tool 削除フィルター] ---
            # ローカルLLMには不要かつ1万トークン以上消費する記憶ツール群を安全に除外
            data['tools'] = [
                t for t in data['tools']
                if not t.get('function', {}).get('name', '').startswith('memory_')
            ]

            # [C-5] アンカーなし正規表現の除去（ローカルLLMの互換性確保）
            remove_unanchored_patterns(data['tools'])

        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in ['host', 'content-length']}

        if is_stream:
            # --- [FAKE STREAMING ARCHITECTURE] ---
            # Qwen 2.5 Coder 14B 等のフォーマット揺れ（<action> ```json など）を
            # 完璧にOpenAIフォーマットへ補正するため、あえてバックエンドには非ストリーミングで
            # 全文を一括生成させ、プロキシ側で修復したあとに、偽のストリームチャンクとして
            # VS Code (Kilo Code) に流し込む。
            data['stream'] = False

            def generate():
                dummy = {
                    "id": "chatcmpl-dummy",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "qwen",
                    "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]
                }
                yield f'data: {json.dumps(dummy)}\n\n'

                q = queue.Queue()

                def reader():
                    try:
                        resp = requests.post(url, json=data, headers=headers, timeout=3600)
                        resp.raise_for_status()
                        q.put(resp.json())
                    except Exception as e:
                        logger.error(f"[FAKE-STREAM] バックエンド接続エラー: {e}")
                        q.put(e)
                
                threading.Thread(target=reader, daemon=True).start()

                start_time = time.time()
                last_heartbeat_time = time.time()

                while True:
                    try:
                        res = q.get(timeout=1.0)
                        
                        if isinstance(res, Exception):
                            error_msg = (
                                f"⚠️ **【サーバー切断 (GPUクラッシュ等)】** ⚠️\n\n"
                                f"LLMサーバーとの通信が切断されました（理由: {res}）。\n"
                                f"重すぎるコンテキストによるGPUクラッシュの可能性があります。\n\n"
                                f"**【対処方法】**\n"
                                f"1. サーバー自体は `systemd` により既に自動再起動しています。\n"
                                f"2. 現在の重すぎる記憶をリセットするため、VS Codeで**「＋（New Chat）」**を開いてください。\n"
                                f"3. 「作業を再開して」とだけ伝えると、AIが `status.md` を読み込んで安全に再開します。"
                            )
                            error_chunk = {
                                "id": "chatcmpl-error",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "qwen",
                                "choices": [{"index": 0, "delta": {"content": error_msg}, "finish_reason": "stop"}]
                            }
                            yield f'data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n'
                            yield 'data: [DONE]\n\n'
                            break
                        
                        # --- [修復処理] ---
                        fixed_resp = fix_tool_calls(res)
                        msg = fixed_resp['choices'][0].get('message', {})
                        content = msg.get('content', '') or ''
                        tool_calls = msg.get('tool_calls')
                        finish_reason = fixed_resp['choices'][0].get('finish_reason', 'stop')

                        # 1. 思考プロセス（content）を先にストリーム
                        if content:
                            chunk = {
                                "id": "chatcmpl-fake-content",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "qwen",
                                "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]
                            }
                            yield f'data: {json.dumps(chunk)}\n\n'
                        
                        # 2. ツール呼び出し（tool_calls）をストリーム
                        if tool_calls:
                            # OpenAIのストリーミング形式に合わせてindexを付与
                            for i, tc in enumerate(tool_calls):
                                tc['index'] = i
                                
                            chunk = {
                                "id": "chatcmpl-fake-tools",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "qwen",
                                "choices": [{"index": 0, "delta": {"tool_calls": tool_calls}, "finish_reason": finish_reason}]
                            }
                            yield f'data: {json.dumps(chunk)}\n\n'
                        else:
                            # ツールがない場合
                            chunk = {
                                "id": "chatcmpl-fake-end",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "qwen",
                                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]
                            }
                            yield f'data: {json.dumps(chunk)}\n\n'

                        yield 'data: [DONE]\n\n'
                        break

                    except queue.Empty:
                        elapsed = time.time() - start_time
                        if (time.time() - last_heartbeat_time) >= HEARTBEAT_INTERVAL_SEC:
                            heartbeat = {
                                "id": "chatcmpl-heartbeat",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": "qwen",
                                "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]
                            }
                            yield f'data: {json.dumps(heartbeat)}\n\n'
                            last_heartbeat_time = time.time()
                            logger.info(f"[FAKE-STREAM HEARTBEAT] {elapsed:.0f}秒経過、Heartbeat送信")

            return Response(generate(), mimetype='text/event-stream')

        else:
            # 非ストリーミングモード
            data['stream'] = False
            try:
                resp = requests.post(url, json=data, headers=headers, timeout=3600)
            except requests.exceptions.RequestException as e:
                logger.error(f"[NON-STREAM] バックエンド接続エラー: {e}")
                error_msg = (
                    f"⚠️ **【サーバー切断 (GPUクラッシュ等)】** ⚠️\n\n"
                    f"LLMサーバーとの通信が切断されました（理由: {e}）。\n"
                    f"重すぎるコンテキストによるGPUクラッシュの可能性があります。\n\n"
                    f"**【対処方法】**\n"
                    f"1. サーバー自体は既に自動再起動しています。\n"
                    f"2. 現在の重すぎる記憶をリセットするため、**「＋（New Chat）」**を開いて作業を再開してください。"
                )
                return Response(
                    json.dumps({
                        "id": "chatcmpl-error",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": "proxy",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": error_msg}, "finish_reason": "stop"}]
                    }, ensure_ascii=False),
                    mimetype='application/json'
                )
            if resp.status_code == 200:
                resp_data = fix_tool_calls(resp.json())
                return Response(json.dumps(resp_data), mimetype='application/json')
            return Response(resp.content, status=resp.status_code, headers=dict(resp.headers))

    # チャット以外のリクエスト（/v1/models 等）は直接プロキシ
    req_kwargs = {
        'method': request.method,
        'url': url,
        'headers': {k: v for k, v in request.headers.items()
                    if k.lower() not in ['host', 'content-length']},
    }
    if request.is_json:
        req_kwargs['json'] = request.get_json(silent=True)
    elif request.data:
        req_kwargs['data'] = request.data

    resp = requests.request(**req_kwargs)
    return Response(resp.content, status=resp.status_code, headers=dict(resp.headers))


if __name__ == '__main__':
    print("Starting Kilo Code Proxy on port 9091...")
    print("Please set Kilo Code Base URL to: http://localhost:9091/v1")
    # [A-1] ローカルループバックのみにバインド（セキュリティ対策）
    app.run(port=9091, host='127.0.0.1')
