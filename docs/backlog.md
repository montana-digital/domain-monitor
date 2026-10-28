# Later work

These items are not implemented. They are not a description of how Domain Monitor works today. Current behavior is in the other documents in this directory.

Pick one item at a time, in the order below unless a later item is the one you were asked to do. Update this file and the design record in the same change when an item lands.

## 9. Add a migration step with the next schema change

`Base.metadata.create_all` creates missing tables. It does not add a column to a table that already exists. An old `monitor.db` keeps its old shape.

Do this when a column or table is actually added. Record a schema version and apply the change to an existing file. An empty migration framework with no change to ship is not the goal.

Files: [app/db.py](../app/db.py), [app/models.py](../app/models.py).

Done when an existing database gains the new column on startup, and a new database still starts clean.
