from __future__ import annotations

import argparse
import json
from pathlib import Path

from .account_store import accounts_as_rows, create_account, delete_account, resolve_account, set_default_account
from .ai_client import load_dotenv
from .ai_providers import add_provider, delete_provider, providers_as_rows
from .scan_service import run_scan
from .scan_store import create_scan_run, list_scan_runs, mark_scan_failed, mark_scan_running, mark_scan_stage, mark_scan_succeeded


def main() -> None:
    parser = argparse.ArgumentParser(description="Longbridge + yfinance + OpenAI-compatible AI single-leg option scanner")
    parser.add_argument("query", nargs="?", help="Natural language scan request, e.g. 扫描NVDA最近的日K线...")
    parser.add_argument("--symbol", help="Override ticker, e.g. NVDA")
    parser.add_argument("--lb-account", "--longbridge-account", dest="longbridge_account", help="Longbridge account profile name")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI and print deterministic fallback report")
    parser.add_argument("--council", action="store_true", help="Run three AI advisor sessions, then synthesize one final plan")
    parser.add_argument("--ai", default="deepseek", help="AI provider name from ai_providers.json, default: deepseek")
    parser.add_argument("--list-ai", action="store_true", help="List configured OpenAI-compatible AI providers")
    parser.add_argument("--add-ai", metavar="NAME", help="Add or update an AI provider")
    parser.add_argument("--delete-ai", metavar="NAME", help="Delete a configured AI provider")
    parser.add_argument("--list-lb-accounts", action="store_true", help="List Longbridge account profiles")
    parser.add_argument("--list-scans", action="store_true", help="List recent scan history")
    parser.add_argument("--add-lb-account", metavar="NAME", help="Add or update a Longbridge account profile")
    parser.add_argument("--delete-lb-account", metavar="NAME", help="Delete a Longbridge account profile")
    parser.add_argument("--set-default-lb-account", metavar="NAME", help="Set the default Longbridge account profile")
    parser.add_argument("--lb-label", help="Display label when adding a Longbridge account profile")
    parser.add_argument("--base-url", help="Provider base URL, e.g. https://api.deepseek.com")
    parser.add_argument("--model", help="Provider model, e.g. deepseek-v4-flash")
    parser.add_argument("--api-key-env", help="Environment variable name containing the provider API key")
    parser.add_argument("--temperature", type=float, default=0.25, help="Provider temperature when adding AI")
    parser.add_argument("--json-out", type=Path, help="Write raw scan payload to a JSON file")
    args = parser.parse_args()

    load_dotenv(Path.cwd() / ".env")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")

    if args.list_ai:
        print(json.dumps(providers_as_rows(), ensure_ascii=False, indent=2))
        return

    if args.list_lb_accounts:
        print(json.dumps(accounts_as_rows(), ensure_ascii=False, indent=2))
        return

    if args.list_scans:
        print(json.dumps(list_scan_runs(30), ensure_ascii=False, indent=2))
        return

    if args.add_lb_account:
        try:
            account = create_account(args.add_lb_account, args.lb_label)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(account.__dict__, ensure_ascii=False, indent=2))
        return

    if args.delete_lb_account:
        try:
            delete_account(args.delete_lb_account)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(accounts_as_rows(), ensure_ascii=False, indent=2))
        return

    if args.set_default_lb_account:
        try:
            account = set_default_account(args.set_default_lb_account)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(account.__dict__, ensure_ascii=False, indent=2))
        return

    if args.add_ai:
        missing = [name for name in ("base_url", "model", "api_key_env") if getattr(args, name) is None]
        if missing:
            parser.error("--add-ai requires --base-url, --model and --api-key-env")
        config_path = add_provider(args.add_ai, args.base_url, args.model, args.api_key_env, args.temperature)
        print(f"AI provider `{args.add_ai}` saved to {config_path}")
        return

    if args.delete_ai:
        try:
            config_path = delete_provider(args.delete_ai)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"AI provider `{args.delete_ai}` deleted from {config_path}")
        return

    if not args.query:
        parser.error("query is required unless using --list-ai, --add-ai or --delete-ai")

    account = resolve_account(args.longbridge_account)
    scan_row = create_scan_run(
        query=args.query,
        symbol=args.symbol,
        ai_provider=args.ai,
        longbridge_account=account.name,
        use_ai=not args.no_ai,
        council=args.council,
    )
    mark_scan_running(scan_row["id"])
    try:
        result = run_scan(
            query=args.query,
            symbol=args.symbol,
            ai_provider=args.ai,
            longbridge_account=account.name,
            use_ai=not args.no_ai,
            council=args.council,
            progress_callback=lambda stage, progress: mark_scan_stage(scan_row["id"], stage, progress),
        )
    except Exception as exc:
        mark_scan_failed(scan_row["id"], str(exc))
        raise
    mark_scan_succeeded(scan_row["id"], result)

    if args.json_out:
        args.json_out.write_text(json.dumps(result["payload"], ensure_ascii=False, indent=2))

    print(result["answer"])


if __name__ == "__main__":
    main()
