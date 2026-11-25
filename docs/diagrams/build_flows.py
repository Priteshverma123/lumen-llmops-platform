"""Simple, plain-language flow + user-flow diagrams → docs/diagrams/lumen-flows.drawio

Complements the detailed technical diagrams (lumen.drawio). Run: python build_flows.py
"""

from pathlib import Path

from _drawio import Document

d = Document()

d.page("1. One LLM call, observed").flow(
    "What happens to one LLM call end to end",
    [("app", "App calls an LLM", "Atlas, Relay, any service", 0, 1, "actor"),
     ("span", "Trace sent", "OpenTelemetry, no code changes", 1, 1),
     ("safe", "Saved safely first", "raw batch kept in storage", 2, 1),
     ("norm", "Cleaned up", "standard names, tokens, cost", 3, 1),
     ("db", "Stored for analysis", "fast columnar database", 4, 1),
     ("dash", "Dashboards", "cost, latency, quality", 5, 0, "good"),
     ("alert", "Alerts", "spikes, drops, drift", 5, 1),
     ("eval", "Quality checks", "sampled answers scored", 5, 2)],
    [("app", "span"), ("span", "safe"), ("safe", "norm"), ("norm", "db"), ("db", "dash"), ("db", "alert"),
     ("db", "eval")],
    note="Data is stored before it is acknowledged: nothing is lost if the database is briefly down.")

d.page("2. User flow: ship a prompt").flow(
    "Changing a prompt safely",
    [("edit", "Edit prompt", "in the registry", 0, 1, "actor"),
     ("ver", "New version saved", "old ones never change", 1, 1),
     ("test", "Run tests", "on a saved dataset", 2, 1),
     ("pass", "Passed?", "", 3, 1, "ask"),
     ("fix", "Fix and retry", "", 3, 2.6, "end"),
     ("ab", "A/B test", "small share of real traffic", 4, 1),
     ("result", "Result?", "", 5, 1, "ask"),
     ("stop", "Auto-stop", "harm detected → rollback", 5, 2.6, "end"),
     ("live", "Promote to production", "apps pick it up within 60s", 6, 1, "good")],
    [("edit", "ver"), ("ver", "test"), ("test", "pass"), ("pass", "fix", "no"), ("fix", "edit"),
     ("pass", "ab", "yes"), ("ab", "result"), ("result", "stop", "worse"), ("result", "live", "better")],
    note="Statistics are valid even if you check results every day (sequential testing).")

d.page("3. User flow: cost spike").flow(
    "Investigating a cost spike",
    [("alert", "Alert fires", "spend 3× normal", 0, 1, "actor"),
     ("dash", "Open cost view", "", 1, 1),
     ("filter", "Narrow it down", "by app, model, prompt version", 2, 1),
     ("traces", "Open example calls", "step-by-step timeline", 3, 1),
     ("cause", "Find the cause", "e.g. cache stopped working", 4, 1),
     ("fix", "Fix + note it", "rollback prompt / config", 5, 1, "good")],
    [("alert", "dash"), ("dash", "filter"), ("filter", "traces"), ("traces", "cause"), ("cause", "fix")])

d.page("4. Quality loop").flow(
    "Keeping answer quality honest",
    [("ans", "Real answers", "sample of production", 0, 1, "actor"),
     ("cheap", "Quick checks", "format, PII, refusals", 1, 1),
     ("judge", "AI judge", "is it backed by sources?", 2, 1),
     ("low", "Low score?", "", 3, 1, "ask"),
     ("ok", "Counted in dashboards", "", 3, 2.6, "good"),
     ("queue", "Human review queue", "", 4, 1),
     ("label", "Human label", "", 5, 1),
     ("data", "Test dataset", "used before every release", 6, 0, "good"),
     ("cal", "Check the judge", "does it agree with humans?", 6, 2)],
    [("ans", "cheap"), ("cheap", "judge"), ("judge", "low"), ("low", "ok", "no"), ("low", "queue", "yes"),
     ("queue", "label"), ("label", "data"), ("label", "cal")])

d.page("5. Embedding request").flow(
    "Turning text into vectors, efficiently",
    [("app", "App sends text", "query or document", 0, 1, "actor"),
     ("seen", "Seen before?", "", 1, 1, "ask"),
     ("hit", "Return saved vector", "instant, free", 1, 2.6, "good"),
     ("wait", "Join a batch", "waits a few ms at most", 2, 1),
     ("prio", "Queries first", "documents can wait", 3, 1),
     ("model", "Model server", "one call for many texts", 4, 1),
     ("save", "Save + return", "", 5, 1, "good")],
    [("app", "seen"), ("seen", "hit", "yes"), ("seen", "wait", "no"), ("wait", "prio"), ("prio", "model"),
     ("model", "save")])

d.page("6. Admin flow").flow(
    "Onboarding a new service",
    [("proj", "Create project", "+ API key", 0, 1, "actor"),
     ("sdk", "Install SDK", "or point OpenTelemetry at Lumen", 1, 1),
     ("prompt", "Move prompts in", "registry + labels", 2, 1),
     ("price", "Check prices", "versioned price list", 3, 1),
     ("rules", "Set alert rules", "latency, cost, quality", 4, 1),
     ("live", "See traces", "within seconds", 5, 1, "good")],
    [("proj", "sdk"), ("sdk", "prompt"), ("prompt", "price"), ("price", "rules"), ("rules", "live")])

print(f"wrote {d.save(Path(__file__).with_name('lumen-flows.drawio'))}")
