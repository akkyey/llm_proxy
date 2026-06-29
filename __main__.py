#!/usr/bin/env python3
"""LLM Proxy 共通エントリポイント

systemd テンプレートユニット (llm-proxy@.service) から呼び出され、
--mode 引数に応じて kilo_proxy または aider_proxy を動的にロードする。

使用例:
    python -m llm_proxy --mode kilo    # Kilo Code (Cline) 用プロキシを起動
    python -m llm_proxy --mode aider   # Aider 用プロキシを起動
"""
import argparse
import importlib
import sys


__version__ = "0.1.0"
# 各モードに対応するモジュール名とデフォルトポートの定義
MODES = {
    "kilo": {
        "module": "kilo_proxy",
        "port": 9091,
        "description": "Kilo Code (Cline) Proxy",
    },
    "aider": {
        "module": "aider_proxy",
        "port": 9092,
        "description": "Aider Proxy",
    },
}


def main():
    parser = argparse.ArgumentParser(
        description="LLM Proxy — ローカルLLM向けプロキシサーバー"
    )
    parser.add_argument(
        "--mode",
        choices=MODES.keys(),
        help="起動するプロキシのモード (kilo / aider)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="バージョンを表示して終了",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="リッスンポート番号（省略時はモードのデフォルト値を使用）",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="バインドするホストアドレス（デフォルト: 127.0.0.1）",
    )
    args = parser.parse_args()

    if not args.mode:
        parser.error("引数 --mode は必須です。")

    mode_config = MODES[args.mode]
    port = args.port or mode_config["port"]

    # モジュールを動的にロード
    try:
        mod = importlib.import_module(mode_config["module"])
    except ImportError as e:
        print(f"エラー: モジュール '{mode_config['module']}' をロードできません: {e}", file=sys.stderr)
        sys.exit(1)

    # Flask アプリケーションを取得して起動
    flask_app = getattr(mod, "app", None)
    if flask_app is None:
        print(f"エラー: モジュール '{mode_config['module']}' に Flask app が見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"Starting {mode_config['description']} v{__version__} on {args.host}:{port}...")
    flask_app.run(port=port, host=args.host)


if __name__ == "__main__":
    main()
