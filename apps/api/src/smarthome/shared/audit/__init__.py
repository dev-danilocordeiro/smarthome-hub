"""Append-only audit log shared by every module.

Modules depend on the `AuditLog` port and append inside their own unit of work, so an
audit entry exists if and only if the change it describes was committed.
"""

from smarthome.shared.audit.model import AuditEntry, AuditEvent, AuditLog, ChainBreak

__all__ = ["AuditEntry", "AuditEvent", "AuditLog", "ChainBreak"]
