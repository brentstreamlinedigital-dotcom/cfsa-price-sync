"""
CFSA Price Sync — main orchestrator.

Run modes:
  python -m src.main                   # process all active suppliers
  python -m src.main --supplier engel  # single supplier
  python -m src.main --dry-run         # parse + diff, no writes
  python -m src.main --trigger email   # hint from Gmail Pub/Sub webhook
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from .automation_status import StatusWriter
from .config_loader import SupplierConfig, load_all_supplier_configs, load_app_config
from .diff_engine import DiffResult, compute_diff
from .email_poller import GmailPoller
from .firebase_logger import SyncLogger
from .normalizer import normalize
from .parsers.router import parse_file
from .sheets_client import SheetsClient
from .shopify_client import ShopifyClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


def main(
    supplier_filter: Optional[str] = None,
    dry_run: bool = False,
    trigger: str = "schedule",
) -> None:
    start_time = time.time()
    app_cfg = load_app_config()

    # ------------------------------------------------------------------ #
    # 1. Load supplier configs
    # ------------------------------------------------------------------ #
    configs = load_all_supplier_configs()
    sw = StatusWriter("price_sync", total=len(configs))
    sw.__enter__()
    if supplier_filter:
        configs = {k: v for k, v in configs.items() if k == supplier_filter}
        if not configs:
            raise ValueError(f"No active supplier config found for: {supplier_filter!r}")

    active_keys = list(configs.keys())
    log.info("Starting sync for suppliers: %s (dry_run=%s)", active_keys, dry_run)

    # ------------------------------------------------------------------ #
    # 2. Init clients
    # ------------------------------------------------------------------ #
    sa_file = app_cfg.get("google", {}).get("service_account_file") or os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS"
    )

    # Env vars take priority over config file values so the repo can be
    # public without exposing any real credentials.
    spreadsheet_id = (
        os.getenv("SHEETS_SPREADSHEET_ID")
        or app_cfg["google"]["sheets"]["spreadsheet_id"]
    )
    if not spreadsheet_id:
        raise RuntimeError("SHEETS_SPREADSHEET_ID env var is not set")
    sheets = SheetsClient(spreadsheet_id, service_account_file=sa_file)

    shopify_cfg = app_cfg.get("shopify", {})
    shop_domain = os.getenv("SHOPIFY_SHOP_DOMAIN") or shopify_cfg.get("shop_domain", "")
    if not shop_domain:
        raise RuntimeError("SHOPIFY_SHOP_DOMAIN env var is not set")
    shopify = ShopifyClient(
        shop_domain=shop_domain,
        access_token=_get_secret_or_env("SHOPIFY_ACCESS_TOKEN", sa_file),
        api_version=shopify_cfg.get("api_version", "2024-10"),
    )

    # Global Shopify location ID — used as fallback when supplier config omits it
    location_id = (
        os.getenv("SHOPIFY_LOCATION_ID")
        or shopify_cfg.get("location_id", "")
    )

    firebase_logger = SyncLogger(
        service_account_file=sa_file,
        collections=app_cfg.get("firebase", {}).get("collections"),
    )
    run_id = firebase_logger.start_run(active_keys, trigger=trigger)
    log.info("Run ID: %s", run_id)

    # ------------------------------------------------------------------ #
    # 3. Read current master sheet
    # ------------------------------------------------------------------ #
    master_df = sheets.read_master()

    # ------------------------------------------------------------------ #
    # 4. Collect incoming data per supplier (email + scrape fallback)
    # ------------------------------------------------------------------ #
    supplier_data: dict[str, pd.DataFrame] = {}

    # 4a. Email attachments
    gmail_cfg = app_cfg.get("google", {}).get("gmail", {})
    email_suppliers = {k: v for k, v in configs.items() if v.source.type == "email"}

    if email_suppliers:
        try:
            delegate_email = (
                os.getenv("GMAIL_DELEGATE_EMAIL")
                or gmail_cfg.get("delegate_email", "")
            )
            poller = GmailPoller(
                delegate_email=delegate_email,
                processed_label=gmail_cfg.get("processed_label", "cfsa/processed"),
                service_account_file=sa_file,
            )
            emails = poller.fetch_supplier_emails(email_suppliers)
            log.info("Fetched %d supplier emails", len(emails))
        except Exception as exc:
            log.warning("Gmail polling failed (%s) — skipping email suppliers", exc)
            emails = []

        for supplier_email in emails:
            key = supplier_email.supplier_key
            cfg = configs[key]

            for att in supplier_email.attachments:
                log.info(
                    "[%s] Parsing attachment: %s", key, att.filename
                )
                try:
                    raw_df = parse_file(
                        att.content_bytes,
                        filename=att.filename,
                        sheet_name=cfg.sheet_name,
                        skip_rows=cfg.skip_rows,
                    )
                    normalized = normalize(raw_df, cfg, source="email")
                    # Merge if multiple attachments from same supplier
                    if key in supplier_data:
                        supplier_data[key] = pd.concat(
                            [supplier_data[key], normalized], ignore_index=True
                        ).drop_duplicates(subset=["sku"])
                    else:
                        supplier_data[key] = normalized

                except Exception as e:
                    log.error("[%s] Failed to parse %s: %s", key, att.filename, e)
                    firebase_logger.log_error(key, "?", "parse_error", str(e))

            if key in supplier_data and not dry_run:
                poller.mark_processed(supplier_email.message_id)

    # 4b. Scrape fallback for suppliers with no email data (or scrape-only)
    for key, cfg in configs.items():
        should_scrape = False

        if cfg.source.type == "scrape":
            should_scrape = True
        elif cfg.scrape_fallback.enabled and key not in supplier_data:
            # No email received — check days_threshold
            days_since_email = _days_since_last_email(master_df, key)
            if days_since_email >= cfg.scrape_fallback.days_threshold:
                log.info(
                    "[%s] No email in %d days — activating scraper",
                    key, days_since_email,
                )
                should_scrape = True

        if should_scrape and cfg.scrape_fallback.url:
            try:
                from scrapers.playwright_scraper import run_scraper
                raw_df = run_scraper(cfg.model_dump())
                normalized = normalize(raw_df, cfg, source="scrape")
                supplier_data[key] = normalized
                log.info("[%s] Scraped %d rows", key, len(normalized))
            except Exception as e:
                log.error("[%s] Scrape failed: %s", key, e)
                firebase_logger.log_error(key, "?", "scrape_error", str(e))

    # ------------------------------------------------------------------ #
    # 5. Diff, write master, sync Shopify
    # ------------------------------------------------------------------ #
    alert_cfg = app_cfg.get("alerts", {})
    price_alert_threshold = alert_cfg.get("price_change_threshold_pct", 15.0)
    increase_threshold_pct = app_cfg.get("pricing", {}).get("increase_threshold_pct", 2.0)
    shopify_write_cap = app_cfg.get("sync", {}).get("shopify_write_cap", 500)
    # location_id already resolved from env/config above in the init block

    all_alerts: list[dict] = []
    error_flag_rows: list[dict] = []
    run_summary: dict[str, dict] = {}

    for _sup_idx, (key, incoming_df) in enumerate(supplier_data.items()):
        cfg = configs[key]
        t0 = time.time()
        log.info("[%s] Processing %d normalized rows", key, len(incoming_df))
        sw.tick(done=_sup_idx, current=key, stage=f"Processing {key} ({_sup_idx+1}/{len(supplier_data)})")

        # Diff
        diff: DiffResult = compute_diff(
            incoming=incoming_df,
            master=master_df,
            supplier=key,
            price_alert_threshold_pct=price_alert_threshold,
            increase_threshold_pct=increase_threshold_pct,
        )
        all_alerts.extend(diff.alerts)

        rows_written = 0
        shopify_failed: list[dict] = []
        synced_at = datetime.now(timezone.utc).isoformat()

        if not dry_run and diff.has_changes:
            # Write new + changed rows to master sheet
            to_write = pd.concat(
                [df for df in [diff.new_rows, diff.changed_rows] if not df.empty],
                ignore_index=True,
            )
            rows_written = sheets.upsert_rows(to_write, supplier=key)

            # New rows with no Shopify variant ID → new_products sheet for review
            if not diff.new_rows.empty:
                vid = diff.new_rows.get("shopify_variant_id")
                no_variant = diff.new_rows[vid.isna() | (vid == "")] if vid is not None else diff.new_rows
                if not no_variant.empty:
                    sheets.append_new_products(no_variant.to_dict("records"))

            # Log price changes to Firestore + price_changes sheet
            if not diff.changed_rows.empty:
                _log_price_changes(firebase_logger, key, diff.changed_rows, master_df)
                # Build sheet records: join incoming rows with old master data
                master_by_sku = master_df[master_df["supplier"] == key].set_index("sku") if not master_df.empty else pd.DataFrame()
                change_records = []
                for _, row in diff.changed_rows.iterrows():
                    sku = str(row.get("sku", ""))
                    old_row = master_by_sku.loc[sku] if sku in master_by_sku.index else None
                    change_records.append({
                        "supplier": key,
                        "sku": sku,
                        "description": row.get("description", ""),
                        "old_price": old_row["selling_price"] if old_row is not None else None,
                        "new_price": row.get("selling_price"),
                        "old_stock_status": old_row["stock_status"] if old_row is not None else None,
                        "new_stock_status": row.get("stock_status"),
                        "alerted": row.get("_price_alerted", False),
                    })
                sheets.append_price_changes(change_records)

            # Sync to Shopify — skip rows that triggered price alerts
            if "_price_alerted" in to_write.columns:
                shopify_rows = to_write[~to_write["_price_alerted"].fillna(False).astype(bool)].to_dict("records")
            else:
                shopify_rows = to_write.to_dict("records")

            if shopify_rows:
                if len(shopify_rows) > shopify_write_cap:
                    log.warning(
                        "[%s] Capping Shopify sync at %d rows (got %d)",
                        key, shopify_write_cap, len(shopify_rows),
                    )
                    shopify_rows = shopify_rows[:shopify_write_cap]

                shopify_failed = shopify.bulk_sync(
                    shopify_rows,
                    location_id=cfg.shopify.location_id or location_id,
                    sync_price=cfg.shopify.sync_price,
                    sync_inventory=cfg.shopify.sync_inventory,
                )

                # Update shopify_last_synced timestamp in master
                for row in shopify_rows:
                    if row.get("sku"):
                        sheets.update_shopify_sync_timestamp(
                            row["sku"], key, synced_at
                        )

        elif dry_run:
            log.info(
                "[%s] DRY RUN — would write %d rows, sync %d to Shopify",
                key, diff.total_changes, diff.total_changes,
            )

        # ── Build price_sync_log entries for every processed product ─────
        # We log ALL rows (new, changed, skipped, unchanged) so the dashboard
        # provides a complete per-run audit trail.
        if not dry_run:
            sync_log_rows = _build_sync_log_rows(
                supplier=key,
                diff=diff,
                incoming_df=incoming_df,
                master_df=master_df,
                synced_at=synced_at if not dry_run else "",
            )
            if sync_log_rows:
                try:
                    sheets.append_price_sync_log(sync_log_rows)
                except Exception as exc:
                    log.warning("[%s] price_sync_log write failed (non-fatal): %s", key, exc)

        # ── Purge stale rows after any scrape run ────────────────────────
        # When data comes from a live scrape (not an email attachment), the
        # scrape represents the supplier's full current catalog. Any master
        # row NOT in the current scrape is stale (filtered-out accessory,
        # discontinued product, or erroneously inserted) and should be
        # removed so the sheet stays in sync with the live data.
        # Safety guard: only purge if we got at least 1 row from the scrape.
        data_source = (
            incoming_df["source"].iloc[0]
            if not incoming_df.empty and "source" in incoming_df.columns
            else "email"
        )
        should_purge = (
            data_source == "scrape"
            and cfg.scrape_fallback.enabled
            and not incoming_df.empty
            and not dry_run
        )
        if should_purge:
            current_skus = set(incoming_df["sku"].dropna().astype(str).tolist())
            purged = sheets.purge_stale_rows(supplier=key, keep_skus=current_skus)
            if purged:
                log.info("[%s] Purged %d stale rows (accessories/discontinued no longer in scrape)", key, purged)

        # Collect error flags
        for failed in shopify_failed:
            firebase_logger.log_error(key, failed.get("sku", "?"), "shopify_rejected", failed.get("error", ""))
            error_flag_rows.append({
                "run_id": run_id,
                "supplier": key,
                "sku": failed.get("sku", "?"),
                "error_type": "shopify_rejected",
                "detail": failed.get("error", ""),
                "raw_row": str(failed),
                "flagged_at": datetime.now(timezone.utc).isoformat(),
                "resolved": "No",
            })

        for alert in diff.alerts:
            error_flag_rows.append({
                "run_id": run_id,
                "supplier": key,
                "sku": alert["sku"],
                "error_type": "price_alert",
                "detail": alert["reason"],
                "raw_row": str(alert),
                "flagged_at": datetime.now(timezone.utc).isoformat(),
                "resolved": "No",
            })

        duration = round(time.time() - t0, 2)

        # Log to Firestore
        firebase_logger.log_supplier_result(
            supplier=key,
            rows_parsed=len(incoming_df),
            rows_new=len(diff.new_rows),
            rows_changed=len(diff.changed_rows),
            rows_unchanged=diff.unchanged_count,
            rows_errored=len(shopify_failed),
            alert_count=len(diff.alerts),
            duration_seconds=duration,
        )

        # Log to master supplier_log sheet
        if not dry_run:
            sheets.append_supplier_log({
                "run_id": run_id,
                "supplier": key,
                "source": incoming_df["source"].iloc[0] if not incoming_df.empty else "?",
                "rows_parsed": len(incoming_df),
                "rows_new": len(diff.new_rows),
                "rows_changed": len(diff.changed_rows),
                "rows_unchanged": diff.unchanged_count,
                "rows_errored": len(shopify_failed),
                "alerts": len(diff.alerts),
                "duration_seconds": duration,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        # Count applied increases vs decreases for Obsidian/reporting
        master_by_sku_for_counts = (
            master_df[master_df["supplier"] == key].set_index("sku")
            if not master_df.empty else pd.DataFrame()
        )
        inc_applied = dec_applied = 0
        if not diff.changed_rows.empty:
            for _, _cr in diff.changed_rows.iterrows():
                if _cr.get("_price_alerted", False):
                    continue
                _sku = str(_cr.get("sku", ""))
                _old = (
                    master_by_sku_for_counts.loc[_sku]["selling_price"]
                    if _sku in master_by_sku_for_counts.index else None
                )
                _new = _cr.get("selling_price")
                try:
                    if _old is not None and _new is not None:
                        if float(_new) > float(_old):
                            inc_applied += 1
                        elif float(_new) < float(_old):
                            dec_applied += 1
                except (TypeError, ValueError):
                    pass

        run_summary[key] = {
            "parsed": len(incoming_df),
            "new": len(diff.new_rows),
            "changed": len(diff.changed_rows),
            "unchanged": diff.unchanged_count,
            "skipped": len(diff.skipped_rows),
            "increases_applied": inc_applied,
            "decreases_applied": dec_applied,
            "errors": len(shopify_failed),
            "alerts": len(diff.alerts),
        }

    # ------------------------------------------------------------------ #
    # 6. Backfill shopify_variant_id for any newly discovered matches
    # ------------------------------------------------------------------ #
    # Fetch the full SKU→variant map from Shopify once per run and update any
    # master sheet rows that don't yet have a shopify_variant_id.  This keeps
    # the dashboard coverage numbers accurate even for products whose price
    # hasn't changed since they were first scraped.
    if not dry_run:
        try:
            log.info("Backfilling Shopify variant IDs from store catalog…")
            sku_variant_map = shopify.get_sku_to_variant_map()
            backfilled = sheets.backfill_shopify_variant_ids(sku_variant_map)
            if backfilled:
                log.info("Backfilled %d new shopify_variant_id values in master sheet", backfilled)
            else:
                log.info("Backfill complete — no new variant IDs to write")
        except Exception as exc:
            log.warning("Shopify variant backfill failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------ #
    # 6b. Rebuild the denormalized `suppliers` tab (Jarvis-readable view)
    # ------------------------------------------------------------------ #
    # After every supplier sync we refresh the per-supplier catalog so the
    # dashboard + any future agents always see current cost / stock / live
    # status grouped by supplier. Non-fatal if it fails — master is the
    # source of truth.
    if not dry_run:
        try:
            n_rows = sheets.rebuild_suppliers_view()
            log.info("Suppliers view rebuilt: %d product rows", n_rows)
        except Exception as exc:
            log.warning("Suppliers view rebuild failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------ #
    # 7. Write error flags to sheet + send alert email if needed
    # ------------------------------------------------------------------ #
    if error_flag_rows and not dry_run:
        sheets.append_error_flags(error_flag_rows)

    if all_alerts and alert_cfg.get("enabled") and not dry_run:
        alerts_recipient = (
            os.getenv("ALERTS_RECIPIENT")
            or alert_cfg.get("recipient", "")
        )
        _send_alert_email(
            recipient=alerts_recipient,
            alerts=all_alerts,
            run_id=run_id,
        )

    # ------------------------------------------------------------------ #
    # 8. Obsidian log (silent if vault_path not configured)
    # ------------------------------------------------------------------ #
    obsidian_vault = app_cfg.get("obsidian", {}).get("vault_path", "")
    if obsidian_vault:
        _write_obsidian_log(obsidian_vault, run_summary)

    # ------------------------------------------------------------------ #
    # 9. Finish
    # ------------------------------------------------------------------ #
    total_duration = round(time.time() - start_time, 2)
    log.info(
        "Sync complete in %.1fs — summary: %s", total_duration, run_summary
    )

    firebase_logger.finish_run(
        status="success" if not error_flag_rows else "partial",
        summary={**run_summary, "total_duration_seconds": total_duration},
    )
    sw.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _days_since_last_email(master_df: pd.DataFrame, supplier: str) -> int:
    """Estimate days since the last email-sourced update for a supplier."""
    if master_df.empty:
        return 9999
    sub = master_df[
        (master_df["supplier"] == supplier) & (master_df["source"] == "email")
    ]
    if sub.empty:
        return 9999
    try:
        dates = pd.to_datetime(sub["last_updated"], errors="coerce").dropna()
        if dates.empty:
            return 9999
        latest = dates.max()
        delta = datetime.now(timezone.utc) - latest.to_pydatetime().replace(
            tzinfo=timezone.utc
        )
        return delta.days
    except Exception:
        return 9999


def _log_price_changes(
    logger: SyncLogger,
    supplier: str,
    changed_rows: pd.DataFrame,
    master_df: pd.DataFrame,
) -> None:
    master_index = {}
    if not master_df.empty:
        sup_master = master_df[master_df["supplier"] == supplier]
        for _, r in sup_master.iterrows():
            master_index[str(r.get("sku", ""))] = r.get("selling_price")

    for _, row in changed_rows.iterrows():
        sku = str(row.get("sku", ""))
        old_price = master_index.get(sku)
        new_price = row.get("selling_price")
        try:
            old_price = float(old_price) if old_price else None
            new_price = float(new_price) if new_price else None
        except (TypeError, ValueError):
            pass

        logger.log_price_change(
            supplier=supplier,
            sku=sku,
            old_price=old_price,
            new_price=new_price,
            alerted=bool(row.get("_price_alerted")),
        )


def _send_alert_email(recipient: str, alerts: list[dict], run_id: str) -> None:
    """Send a plain-text alert email for large price changes via Gmail API."""
    if not recipient:
        log.warning("Alert email not sent — no recipient configured")
        return
    try:
        import base64
        from email.mime.text import MIMEText
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        body_lines = [
            f"CFSA Price Sync — Run {run_id}",
            f"{len(alerts)} price alert(s) require your review:\n",
        ]
        for a in alerts:
            try:
                line = (
                    f"  [{a['supplier']}] {a['sku']}: "
                    f"R{float(a.get('old_price') or 0):.2f} → R{float(a.get('new_price') or 0):.2f} "
                    f"({a.get('price_delta_pct', 0):+.1f}%)"
                )
            except Exception:
                line = f"  [{a.get('supplier')}] {a.get('sku')}"
            body_lines.append(line)
        body_lines.append("\nCheck the 'error_flags' tab in the master sheet.")

        msg = MIMEText("\n".join(body_lines))
        msg["to"] = recipient
        msg["subject"] = f"[CFSA] Price alerts from sync run {run_id}"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        # Use OAuth refresh token (same credentials as email poller)
        refresh_token = os.getenv("GMAIL_REFRESH_TOKEN")
        client_id = os.getenv("GMAIL_CLIENT_ID")
        client_secret = os.getenv("GMAIL_CLIENT_SECRET")

        if not (refresh_token and client_id and client_secret):
            log.warning("Alert email not sent — GMAIL_REFRESH_TOKEN/CLIENT_ID/CLIENT_SECRET not set")
            return

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        service.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        log.info("Alert email sent to %s", recipient)
    except Exception as e:
        log.error("Failed to send alert email: %s", e)


def _get_secret_or_env(secret_name: str, sa_file: Optional[str] = None) -> str:
    """Try env var first, then Secret Manager."""
    env_val = os.getenv(secret_name)
    if env_val:
        return env_val
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/-/secrets/{secret_name}/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("utf-8")
    except Exception as e:
        raise RuntimeError(
            f"Secret {secret_name!r} not in env and Secret Manager failed: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Price sync log builder
# ---------------------------------------------------------------------------

def _build_sync_log_rows(
    supplier: str,
    diff: "DiffResult",
    incoming_df: "pd.DataFrame",
    master_df: "pd.DataFrame",
    synced_at: str,
) -> list[dict]:
    """
    Build a list of price_sync_log dicts for every product processed in one
    supplier run — new, changed, skipped (below threshold), and unchanged.
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    master_by_sku = (
        master_df[master_df["supplier"] == supplier].set_index("sku")
        if not master_df.empty else pd.DataFrame()
    )
    incoming_by_sku = (
        incoming_df.set_index("sku")
        if not incoming_df.empty and "sku" in incoming_df.columns
        else pd.DataFrame()
    )

    def _fmt_price(val) -> str:
        try:
            return f"{float(val):.2f}" if val is not None and val != "" else ""
        except (TypeError, ValueError):
            return str(val) if val is not None else ""

    rows: list[dict] = []

    # ── New products ──────────────────────────────────────────────────
    if not diff.new_rows.empty:
        for _, row in diff.new_rows.iterrows():
            rows.append({
                "timestamp": now_str,
                "sku": str(row.get("sku", "")),
                "product_name": str(row.get("description", "") or ""),
                "old_supplier_price": "",
                "new_supplier_price": _fmt_price(row.get("selling_price")),
                "pct_change": "",
                "direction": "new",
                "applied": "true",
                "skip_reason": "",
                "shopify_updated_at": synced_at if row.get("shopify_variant_id") else "",
            })

    # ── Changed rows (incl. alerted) ─────────────────────────────────
    if not diff.changed_rows.empty:
        for _, row in diff.changed_rows.iterrows():
            sku = str(row.get("sku", ""))
            old_p = (
                master_by_sku.loc[sku]["selling_price"]
                if sku in master_by_sku.index else None
            )
            new_p = row.get("selling_price")
            alerted = bool(row.get("_price_alerted", False))

            try:
                old_f = float(old_p) if old_p is not None and old_p != "" else None
                new_f = float(new_p) if new_p is not None and new_p != "" else None
                if old_f is not None and new_f is not None and old_f != 0:
                    pct = (new_f - old_f) / old_f * 100
                    pct_str = f"{pct:+.1f}%"
                    direction = "increase" if pct > 0 else ("decrease" if pct < 0 else "unchanged")
                else:
                    pct_str = ""
                    direction = ""
            except (TypeError, ValueError):
                pct_str = ""
                direction = ""

            rows.append({
                "timestamp": now_str,
                "sku": sku,
                "product_name": str(row.get("description", "") or ""),
                "old_supplier_price": _fmt_price(old_p),
                "new_supplier_price": _fmt_price(new_p),
                "pct_change": pct_str,
                "direction": direction,
                "applied": "false" if alerted else "true",
                "skip_reason": "Price alert flagged for review" if alerted else "",
                "shopify_updated_at": synced_at if (not alerted and row.get("shopify_variant_id")) else "",
            })

    # ── Skipped (below increase threshold) ───────────────────────────
    for sr in diff.skipped_rows:
        sku = str(sr.get("sku", ""))
        desc = str(sr.get("description", "") or "")
        if not desc and sku in incoming_by_sku.index:
            desc = str(incoming_by_sku.loc[sku].get("description", "") or "")
        pct_val = sr.get("pct_change")
        pct_str = f"+{pct_val:.1f}%" if pct_val is not None else ""
        rows.append({
            "timestamp": now_str,
            "sku": sku,
            "product_name": desc,
            "old_supplier_price": _fmt_price(sr.get("old_price")),
            "new_supplier_price": _fmt_price(sr.get("new_price")),
            "pct_change": pct_str,
            "direction": "increase",
            "applied": "false",
            "skip_reason": sr.get("skip_reason", "Below 2% increase threshold"),
            "shopify_updated_at": "",
        })

    # ── Unchanged rows ────────────────────────────────────────────────
    for ur in diff.unchanged_rows:
        sku = str(ur.get("sku", ""))
        price = ur.get("selling_price")
        desc = str(ur.get("description", "") or "")
        if not desc and sku in incoming_by_sku.index:
            desc = str(incoming_by_sku.loc[sku].get("description", "") or "")
        rows.append({
            "timestamp": now_str,
            "sku": sku,
            "product_name": desc,
            "old_supplier_price": _fmt_price(price),
            "new_supplier_price": _fmt_price(price),
            "pct_change": "0%",
            "direction": "unchanged",
            "applied": "false",
            "skip_reason": "Price unchanged",
            "shopify_updated_at": "",
        })

    return rows


# ---------------------------------------------------------------------------
# Obsidian log writer
# ---------------------------------------------------------------------------

def _write_obsidian_log(vault_path: str, run_summary: dict) -> None:
    """
    Write a markdown price sync summary to the Obsidian vault.
    Path: {vault_path}/CFSA/Price Sync/YYYY-MM-DD_HH-MM_price-sync.md
    Silently skips if vault_path is empty or the path is not writable.
    """
    try:
        try:
            import pytz
            sast = pytz.timezone("Africa/Johannesburg")
            now_sast = datetime.now(sast)
        except ImportError:
            # pytz not installed — fall back to UTC
            now_sast = datetime.now(timezone.utc)

        now_str  = now_sast.strftime("%Y-%m-%d %H:%M SAST")
        file_ts  = now_sast.strftime("%Y-%m-%d_%H-%M")

        processed         = sum(v.get("parsed", 0) for v in run_summary.values())
        increases_applied = sum(v.get("increases_applied", 0) for v in run_summary.values())
        increases_skipped = sum(v.get("skipped", 0) for v in run_summary.values())
        decreases_applied = sum(v.get("decreases_applied", 0) for v in run_summary.values())
        errors            = sum(v.get("errors", 0) for v in run_summary.values())

        content = (
            f"## Price Sync Run — {now_str}\n"
            f"- Products processed: {processed}\n"
            f"- Price increases applied: {increases_applied}\n"
            f"- Price increases skipped (below 2% threshold): {increases_skipped}\n"
            f"- Price decreases applied: {decreases_applied}\n"
            f"- Errors: {errors}\n"
        )

        vault    = Path(vault_path)
        out_dir  = vault / "CFSA" / "Price Sync"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{file_ts}_price-sync.md"
        out_file.write_text(content, encoding="utf-8")
        log.info("Obsidian log written to %s", out_file)

    except Exception as exc:
        log.warning("Obsidian log write failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CFSA Supplier Price Sync")
    parser.add_argument("--supplier", help="Run for a single supplier key only")
    parser.add_argument("--dry-run", action="store_true", help="Parse + diff, no writes")
    parser.add_argument(
        "--trigger",
        default="schedule",
        choices=["schedule", "email_push", "manual"],
        help="What triggered this run",
    )
    args = parser.parse_args()

    main(
        supplier_filter=args.supplier,
        dry_run=args.dry_run,
        trigger=args.trigger,
    )
