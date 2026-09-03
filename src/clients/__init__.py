"""Outbound calls to third parties, one module per service.

Each is the same shape: ask a service, hand back plain data, know nothing about
a trip. They live together so that "what does this app talk to" is answerable
by listing a directory, which matters because a request that escapes a test can
cost money.

Not every outbound call is here. components/search_web.py and
components/place_search.py also reach a paid API, and they are components
because they are capabilities a workflow chains and a test page exercises.
These four are the lower layer those and the workflows are built on.
"""
