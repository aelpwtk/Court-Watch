import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["NTFY_TOPIC"] = "main-topic"
os.environ["NTFY_TOPIC_SHARED"] = "shared-topic"
os.environ["CRYSTAL_GAP"] = "0"

import check_courts, check_crystal

for mod in (check_courts, check_crystal):
    name = mod.__name__
    sent = []
    mod._publish = lambda topic, title, msg, prio, tags: sent.append(topic)

    mod.ntfy("health ping", "x")                       # no share -> only Marc
    assert sent == ["main-topic"], (name, sent)

    sent.clear(); mod.ntfy("court open", "x", share=True)   # court alert -> both
    assert sent == ["main-topic", "shared-topic"], (name, sent)

    def boom_on_shared(topic, *a):
        sent.append(topic)
        if topic == "shared-topic":
            raise OSError("shared down")
    mod._publish = boom_on_shared
    sent.clear(); mod.ntfy("court open", "x", share=True)   # shared failure must not raise
    assert sent == ["main-topic", "shared-topic"], (name, sent)

    def boom_on_main(topic, *a):
        sent.append(topic); raise OSError("main down")
    mod._publish = boom_on_main
    sent.clear()
    try:
        mod.ntfy("court open", "x", share=True); raise SystemExit(f"{name}: should have raised")
    except OSError:
        pass
    assert sent == ["main-topic"], (name, sent)        # main failure raises (caller retries), shared not tried

    mod.SHARED_TOPIC = ""
    mod._publish = lambda topic, *a: sent.append(topic)
    sent.clear(); mod.ntfy("court open", "x", share=True)   # no shared secret -> behaves as before
    assert sent == ["main-topic"], (name, sent)
print("ALL PASS")
