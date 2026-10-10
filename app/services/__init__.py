"""Application services: the single place for database access per domain.

Layering: browser → panel/API routes → service layer → ORM → PostgreSQL.
Routes handle HTTP, auth, and response mapping only; every query and state
change lives here so the panel and the machine API cannot drift apart, and
no route contains ad-hoc SQL. Services never audit (routes do, with their
own user) and never render.
"""
