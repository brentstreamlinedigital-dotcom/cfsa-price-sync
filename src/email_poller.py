"""
Gmail API email poller.

Flow:
1. Search for unread emails matching supplier domain/subject filters
2. Download attachments (CSV, XLSX, PDF)
3. Mark email as processed (adds label, removes from INBOX)
4. Return list of {supplier_key, message_id, attachments: [{filename, content_bytes, ext}]}

Authentication:
- Service account with domain-wide delegation (preferred for Cloud Run)
- Or OAuth2 credentials (local dev)
"""
from __future__ import annotations

import base64
import email.utils
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config_loader import SupplierConfig

log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
SUPPORTED_ATTACHMENT_EXTS = {"csv", "xlsx", "xls", "xlsm", "pdf"}


@dataclass
class EmailAttachment:
    filename: str
    content_bytes: bytes
    ext: str


@dataclass
class SupplierEmail:
    supplier_key: str
    message_id: str
    subject: str
    received_at: str
    attachments: list[EmailAttachment] = field(default_factory=list)


class GmailPoller:
    def __init__(
        self,
        delegate_email: str,
        processed_label: str = "cfsa/processed",
        service_account_file: Optional[str] = None,
    ):
        """
        Args:
            delegate_email:      The Gmail address to poll (service account delegates to it).
            processed_label:     Gmail label applied after processing.
            service_account_file: Path to SA JSON (None = use ADC on Cloud Run).
        """
        self.delegate_email = delegate_email
        self.processed_label = processed_label
        self._service = self._build_service(service_account_file)
        self._processed_label_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_supplier_emails(
        self, configs: dict[str, SupplierConfig]
    ) -> list[SupplierEmail]:
        """
        Search Gmail for unprocessed supplier emails across all active configs.

        Returns list of SupplierEmail with downloaded attachment bytes.
        """
        results: list[SupplierEmail] = []
        label_id = self._get_or_create_label(self.processed_label)

        for supplier_key, cfg in configs.items():
            if cfg.source.type != "email":
                continue

            query = self._build_query(cfg, label_id)
            log.debug("[%s] Gmail query: %s", supplier_key, query)

            try:
                msg_stubs = self._list_messages(query)
            except HttpError as e:
                log.error("[%s] Gmail list error: %s", supplier_key, e)
                continue

            for stub in msg_stubs:
                try:
                    supplier_email = self._process_message(
                        stub["id"], supplier_key, cfg
                    )
                    if supplier_email and supplier_email.attachments:
                        results.append(supplier_email)
                except Exception as e:
                    log.error(
                        "[%s] Failed to process message %s: %s",
                        supplier_key, stub["id"], e,
                    )

        log.info("Found %d supplier emails with attachments", len(results))
        return results

    def mark_processed(self, message_id: str) -> None:
        """Add processed label and archive the email."""
        label_id = self._get_or_create_label(self.processed_label)
        try:
            self._service.users().messages().modify(
                userId="me",
                id=message_id,
                body={
                    "addLabelIds": [label_id],
                    "removeLabelIds": ["INBOX", "UNREAD"],
                },
            ).execute()
            log.debug("Marked message %s as processed", message_id)
        except HttpError as e:
            log.warning("Failed to mark message %s as processed: %s", message_id, e)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_query(self, cfg: SupplierConfig, label_id: str) -> str:
        parts = []

        # From domain filter
        domains = cfg.source.email_from_domains
        if domains:
            from_parts = " OR ".join(f"from:{d}" for d in domains)
            parts.append(f"({from_parts})")

        # Subject filter (optional)
        subjects = cfg.source.email_subject_contains
        if subjects:
            subj_parts = " OR ".join(f'subject:"{s}"' for s in subjects)
            parts.append(f"({subj_parts})")

        # Must have attachment and not already processed
        parts.append("has:attachment")
        parts.append(f"-label:{self.processed_label}")

        return " ".join(parts)

    def _list_messages(self, query: str) -> list[dict]:
        result = (
            self._service.users()
            .messages()
            .list(userId="me", q=query, maxResults=50)
            .execute()
        )
        return result.get("messages", [])

    def _process_message(
        self, message_id: str, supplier_key: str, cfg: SupplierConfig
    ) -> Optional[SupplierEmail]:
        msg = (
            self._service.users()
            .messages()
            .get(userId="me", messageId=message_id, format="full")
            .execute()
        )

        subject = self._get_header(msg, "Subject")
        date_str = self._get_header(msg, "Date")
        received_at = self._parse_date(date_str)

        attachments = self._extract_attachments(msg, cfg)
        if not attachments:
            return None

        return SupplierEmail(
            supplier_key=supplier_key,
            message_id=message_id,
            subject=subject,
            received_at=received_at,
            attachments=attachments,
        )

    def _extract_attachments(
        self, message: dict, cfg: SupplierConfig
    ) -> list[EmailAttachment]:
        allowed_exts = set(cfg.source.attachment_types or SUPPORTED_ATTACHMENT_EXTS)
        attachments: list[EmailAttachment] = []

        parts = self._flatten_parts(message.get("payload", {}))

        for part in parts:
            filename = part.get("filename", "")
            if not filename:
                continue

            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in allowed_exts:
                continue

            body = part.get("body", {})
            attachment_id = body.get("attachmentId")

            if attachment_id:
                att_data = (
                    self._service.users()
                    .messages()
                    .attachments()
                    .get(
                        userId="me",
                        messageId=message["id"],
                        id=attachment_id,
                    )
                    .execute()
                )
                raw = base64.urlsafe_b64decode(att_data["data"])
            elif body.get("data"):
                raw = base64.urlsafe_b64decode(body["data"])
            else:
                continue

            attachments.append(
                EmailAttachment(filename=filename, content_bytes=raw, ext=ext)
            )

        return attachments

    def _flatten_parts(self, payload: dict) -> list[dict]:
        """Recursively flatten multipart email parts."""
        parts = []
        if payload.get("filename") or payload.get("body", {}).get("attachmentId"):
            parts.append(payload)
        for sub in payload.get("parts", []):
            parts.extend(self._flatten_parts(sub))
        return parts

    def _get_or_create_label(self, label_name: str) -> str:
        if self._processed_label_id:
            return self._processed_label_id

        labels = (
            self._service.users().labels().list(userId="me").execute()
        ).get("labels", [])

        for label in labels:
            if label["name"] == label_name:
                self._processed_label_id = label["id"]
                return label["id"]

        # Create label if it doesn't exist
        new_label = (
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
        self._processed_label_id = new_label["id"]
        log.info("Created Gmail label: %s", label_name)
        return new_label["id"]

    def _build_service(self, service_account_file: Optional[str]):
        import os
        refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
        client_id = os.environ.get("GMAIL_CLIENT_ID")
        client_secret = os.environ.get("GMAIL_CLIENT_SECRET")

        if refresh_token and client_id and client_secret:
            # OAuth2 with refresh token (works for personal Gmail)
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=GMAIL_SCOPES,
            )
        elif service_account_file:
            creds = service_account.Credentials.from_service_account_file(
                service_account_file,
                scopes=GMAIL_SCOPES,
                subject=self.delegate_email,
            )
        else:
            import google.auth
            creds, _ = google.auth.default(scopes=GMAIL_SCOPES)
            creds = creds.with_subject(self.delegate_email)

        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    @staticmethod
    def _get_header(message: dict, name: str) -> str:
        headers = message.get("payload", {}).get("headers", [])
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    @staticmethod
    def _parse_date(date_str: str) -> str:
        try:
            parsed = email.utils.parsedate_to_datetime(date_str)
            return parsed.astimezone(timezone.utc).isoformat()
        except Exception:
            return datetime.now(timezone.utc).isoformat()
