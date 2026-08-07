from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import dotenv_values

from market_analysis.history_management.histdata import HistDataPackageLoader
from market_analysis.history_management.postgres import (
    HistoricalDatasetAlreadyExistsError,
    HistoricalPostgresRepository,
)
from market_analysis.infrastructure.postgres.settings import PostgresSettings


def _settings(project_root: Path) -> PostgresSettings:
    values: dict[str, str | None] = {}
    for name in (".env", ".env.local"):
        path = project_root / name
        if path.is_file():
            values.update(dotenv_values(path))
    dsn = (values.get("MARKET_ANALYSIS_DATABASE_URL") or "").strip()
    if not dsn:
        raise RuntimeError("MARKET_ANALYSIS_DATABASE_URL is not configured")
    return PostgresSettings(
        dsn=dsn,
        min_pool_size=int(values.get("MARKET_ANALYSIS_DB_MIN_POOL_SIZE") or "1"),
        max_pool_size=int(values.get("MARKET_ANALYSIS_DB_MAX_POOL_SIZE") or "5"),
        command_timeout_seconds=float(
            values.get("MARKET_ANALYSIS_DB_COMMAND_TIMEOUT_SECONDS") or "10"
        ),
    )


async def _run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parents[1]
    package_root = args.package.resolve()
    loader = HistDataPackageLoader(package_root)
    loaded = [loader.load(symbol) for symbol in args.symbol]
    for result in loaded:
        descriptor = result.bundle.descriptor
        print(
            f"{descriptor.instrument_symbol}: records={result.bundle.record_count}, "
            f"kind={descriptor.record_kind.value}, basis={descriptor.bar_price_basis.value}, "
            f"time_resolution={descriptor.timestamp_resolution}, "
            f"stored_scale={descriptor.storage_price_scale}, "
            f"observed_quantum={descriptor.effective_price_quantum}, "
            f"validation={result.validation.status.value}"
        )
    if not args.apply:
        print("Dry run only; pass --apply to persist validated candidates.")
        return 0

    repository = HistoricalPostgresRepository(_settings(project_root))
    await repository.open()
    try:
        for result in loaded:
            dataset_id = result.bundle.descriptor.dataset_id
            try:
                state = await repository.save_validated_candidate(
                    result.bundle,
                    result.validation,
                )
            except HistoricalDatasetAlreadyExistsError:
                print(f"{dataset_id}: already registered; immutable dataset was not overwritten")
            else:
                print(f"{dataset_id}: persisted with state={state.value}")
    finally:
        await repository.close()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and register downloaded HistData M1 Bid bars as historical "
            "candidates. This command never makes provider requests."
        )
    )
    project_root = Path(__file__).resolve().parents[1]
    parser.add_argument(
        "--package",
        type=Path,
        default=project_root / "data/history/packages/histdata/2026-07",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        choices=("XAUUSD", "XAGUSD"),
        help="symbol to load; defaults to both gold and silver",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="persist validated candidates into the isolated history schema",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if not args.symbol:
        args.symbol = ["XAUUSD", "XAGUSD"]
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
