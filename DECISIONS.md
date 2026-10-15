# Design decisions

1. **Indexes.** Unique `emp_code` and `(emp_code, date)` stop duplicate employees and same-day punches. `(department, emp_code)` lists a department. `(department, joined_on)` and `joined_on` select joiners. `(date, emp_code)` and `(status, date, emp_code)` filter logs. I rejected a second `emp_code`/`date` index and a unique open-punch index, because two dates can both be open.

2. **Punch-in race.** Both requests insert `(emp_code, date)`. The unique index keeps one; `DuplicateKeyError` becomes 409. Punch-out and regularization update only if the read values are unchanged, so the second call is 409 and history stays. One document needs no transaction.

3. **Ties.** `$rank` uses only late minutes, so ties share a rank (1, 2, 2, 4). `limit` keeps rank `<= limit`, so a tie at the cutoff returns extra rows, then `emp_code` order. Department is filtered first.

4. **Headcount.** Summary reads employees joined on or before month end and keeps empty attendance lookups, so zero-log employees count and later joiners do not. A staffed empty working day has rate 0; zero headcount has a null rate.

5. **100x.** Keep the log authoritative and refresh a daily department summary. The per-day headcount lookup is the first limit. Broad filters are hinted so the plan stays an index scan.
