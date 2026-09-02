"""Flask blueprints, one module per subject.

app.py used to hold all 69 routes because @app.route needs the app object,
which put an admin review queue and a parent's trip page in the same file. Each
module here collects its own routes on a Blueprint and app.py registers them,
so a file has one subject and one audience.

Route bodies only. The logic they call still lives in src/components,
src/workflows and the domain modules, which is what kept this a move rather
than a rewrite.
"""
