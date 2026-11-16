"""Preserve historical CRM records written before linked accounts shared an owner."""
from app.sheet_store import SheetStore, is_active_access_state, is_admin_role


class ReviewSheetStore(SheetStore):
    def _linked_scope(self, email: str) -> tuple[set[str], set[str]]:
        owner = email.strip().casefold()
        records = self.access_records()
        principal = next((record for record in records if record.email == owner and
                          is_active_access_state(record.state)), None)
        if not principal or principal.account_email or is_admin_role(principal.role):
            return set(), set()
        aliases = {
            record.email for record in records
            if record.account_email == owner and record.email != owner
            and is_active_access_state(record.state) and not is_admin_role(record.role)
        }
        if not aliases:
            return set(), set()
        return aliases, {source.record_id for source in self.onboarding_sources(owner)}

    def recent_prospects(self, email, limit=50):
        if not email:
            return super().recent_prospects(email, limit)
        aliases, sources = self._linked_scope(email)
        if not aliases:
            return super().recent_prospects(email, limit)
        owner = email.strip().casefold()
        rows = super().recent_prospects(None, limit=1000)
        return [lead for lead in rows if lead["email"].strip().casefold() == owner or
                (lead["email"].strip().casefold() in aliases and lead["onboarding_id"].strip() in sources)][:limit]

    def review_events(self, owner_email=None, *, onboarding_id=None, execution_id=None):
        if not owner_email:
            return super().review_events(onboarding_id=onboarding_id, execution_id=execution_id)
        aliases, sources = self._linked_scope(owner_email)
        if not aliases:
            return super().review_events(owner_email, onboarding_id=onboarding_id, execution_id=execution_id)
        owner = owner_email.strip().casefold()
        return [event for event in super().review_events(onboarding_id=onboarding_id, execution_id=execution_id)
                if event["owner_email"] == owner or
                (event["owner_email"] in aliases and event["onboarding_id"] in sources)]

    def _original_owner(self, execution_id, email):
        prospect = self.get_prospect(execution_id, email)
        if prospect is None:
            raise PermissionError("No puedes modificar un lead de otra cuenta")
        return prospect["email"]

    def update_prospect_status(self, execution_id, email, status, *, is_admin=False):
        owner = email if is_admin else self._original_owner(execution_id, email)
        return super().update_prospect_status(execution_id, owner, status, is_admin=is_admin)

    def update_prospect_crm(self, execution_id, email, *, is_admin=False, **fields):
        owner = email if is_admin else self._original_owner(execution_id, email)
        return super().update_prospect_crm(execution_id, owner, is_admin=is_admin, **fields)
