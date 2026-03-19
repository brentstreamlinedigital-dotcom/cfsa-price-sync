"""
Firebase Firestore audit logger.

Three collections (scoped — product data stays in Google Sheets):
  sync_runs      — one doc per pipeline run
  price_changes  — immutable log of every price change with delta %
  sync_errors    — rows that failed to map or sync, with error type

Usage:
    logger = SyncLogger()
    run_id = logger.start_run(["engel", "arb"])
    logger.log_supplier_result("engel", rows_parsed=50, rows_changed=3, ...)
    logger.log_price_change("engel", "MD14F", old_price=8699, new_price=9200, ...)
    logger.log_error("engel", "BAD_SKU", "mapping_failed", "No column 'SKU' found")
    logger.finish_run("success")
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import credentials, firestore

log = logging.getLogger(__name__)


class SyncLogger:
    def __init__(
        self,
        service_account_file: Optional[str] = None,
        collections: Optional[dict[str, str]] = None,
    ):
        """
        Args:
            service_account_file: Path to SA JSON. None = Application Default Credentials.
            collections:          Override collection names from app config.
        """
        self._init_firebase(service_account_file)
        self.db = firestore.client()

        c = collections or {}
        self.col_runs = c.get("sync_runs", "sync_runs")
        self.col_changes = c.get("price_changes", "price_changes")
        self.col_errors = c.get("sync_errors", "sync_errors")

        self.run_id: str = str(uuid.uuid4())[:8]

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def start_run(self, suppliers: list[str], trigger: str = "schedule") -> str:
        """
        Open a sync_run document. Returns run_id.

        Args:
            suppliers: List of supplier keys being processed this run.
            trigger:   "schedule" | "email_push" | "manual"
        """
        self.db.collection(self.col_runs).document(self.run_id).set(
            {
                "run_id": self.run_id,
                "started_at": datetime.now(timezone.utc),
                "suppliers": suppliers,
                "trigger": trigger,
                "status": "running",
            }
        )
        log.info("Sync run %s started (trigger=%s, suppliers=%s)", self.run_id, trigger, suppliers)
        return self.run_id

    def finish_run(self, status: str = "success", summary: Optional[dict] = None) -> None:
        """
        Close the sync_run document.

        Args:
            status:  "success" | "partial" | "failed"
            summary: Optional dict of aggregate stats to store.
        """
        update = {
            "status": status,
            "finished_at": datetime.now(timezone.utc),
        }
        if summary:
            update["summary"] = summary
        self.db.collection(self.col_runs).document(self.run_id).update(update)
        log.info("Sync run %s finished — status=%s", self.run_id, status)

    # ------------------------------------------------------------------
    # Per-supplier results
    # ------------------------------------------------------------------

    def log_supplier_result(
        self,
        supplier: str,
        rows_parsed: int = 0,
        rows_new: int = 0,
        rows_changed: int = 0,
        rows_unchanged: int = 0,
        rows_errored: int = 0,
        alert_count: int = 0,
        duration_seconds: float = 0.0,
    ) -> None:
        self.db.collection(self.col_runs).document(self.run_id).collection(
            "supplier_results"
        ).document(supplier).set(
            {
                "supplier": supplier,
                "rows_parsed": rows_parsed,
                "rows_new": rows_new,
                "rows_changed": rows_changed,
                "rows_unchanged": rows_unchanged,
                "rows_errored": rows_errored,
                "alert_count": alert_count,
                "duration_seconds": round(duration_seconds, 2),
                "completed_at": datetime.now(timezone.utc),
            }
        )

    # ------------------------------------------------------------------
    # Price change audit trail
    # ------------------------------------------------------------------

    def log_price_change(
        self,
        supplier: str,
        sku: str,
        old_price: Optional[float],
        new_price: Optional[float],
        old_stock_status: Optional[str] = None,
        new_stock_status: Optional[str] = None,
        alerted: bool = False,
    ) -> None:
        """
        Append one immutable price-change record.
        This collection is the audit trail for all price movements.
        """
        delta_pct: Optional[float] = None
        if old_price and old_price != 0 and new_price is not None:
            delta_pct = round((new_price - old_price) / old_price * 100, 2)

        self.db.collection(self.col_changes).add(
            {
                "run_id": self.run_id,
                "supplier": supplier,
                "sku": sku,
                "old_price": old_price,
                "new_price": new_price,
                "price_delta_pct": delta_pct,
                "old_stock_status": old_stock_status,
                "new_stock_status": new_stock_status,
                "alerted": alerted,
                "changed_at": datetime.now(timezone.utc),
            }
        )

    # ------------------------------------------------------------------
    # Error logging
    # ------------------------------------------------------------------

    def log_error(
        self,
        supplier: str,
        sku: str,
        error_type: str,
        detail: str,
    ) -> None:
        """
        Log a row-level error.

        error_type values:
          mapping_failed     — column_map couldn't find required columns
          parse_error        — file could not be parsed
          shopify_rejected   — Shopify API returned an error
          price_alert        — price change exceeded threshold
          missing_sku        — product not in Shopify yet
        """
        self.db.collection(self.col_errors).add(
            {
                "run_id": self.run_id,
                "supplier": supplier,
                "sku": sku,
                "error_type": error_type,
                "detail": detail,
                "logged_at": datetime.now(timezone.utc),
                "resolved": False,
            }
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _init_firebase(service_account_file: Optional[str]) -> None:
        if firebase_admin._apps:
            return  # already initialized
        if service_account_file:
            cred = credentials.Certificate(service_account_file)
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
