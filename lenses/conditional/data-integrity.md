# Data Integrity Lens

## When to apply
Projects with a database, persistent storage, or data pipeline.

## What to look for
- Migrations: are schema changes versioned, reversible, and tested?
- Transactions: are multi-step mutations atomic?
- Validation: is data validated before write, not just on read?
- Backups: is there a backup strategy? Has restore been tested?
- Consistency: can the system end up in an inconsistent state?

## Smells
- Schema changes applied directly to production (no migration files)
- Multi-table updates without transactions
- Data validated only in the UI layer (bypassed by API calls)
- No foreign key constraints on related data
- Soft deletes that leak into queries (WHERE deleted_at IS NULL everywhere)
- Migration files that are modified after being applied to production
- No backup verification — backups exist but have never been restored
- Cascade deletes on high-value data without confirmation

## What good looks like
- Every schema change is a versioned, reviewed migration file
- Writes are validated at the boundary AND enforced by database constraints
- Transactions wrap operations that must succeed or fail together
- Migrations are tested: apply, verify, rollback, verify
- Backup restore is tested on a schedule, not just assumed to work
- Audit trail for sensitive data changes (who changed what, when)

## Questions to ask
- If a migration fails halfway through, what state is the database in?
- Can you restore from backup and verify the data is correct?
- Is there any data that exists only in the database with no way to reconstruct it?
- What's the blast radius of a bad migration?
