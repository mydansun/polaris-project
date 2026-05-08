"""API-side replay surfaces.

Phase 0 stub.  When ``POLARIS_RECORD=<path>`` is set, the web app will
POST user actions to ``/replay/record/append`` and the route forwards
them to the worker's recorder over the same shared fixture path (or
later: a Redis side-channel).  Until Phase 1 lands the receiver, the
route here just ack-200s so the web side can wire its calls now.
"""
