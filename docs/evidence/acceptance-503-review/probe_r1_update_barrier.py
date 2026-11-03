"""仅用临时控制库验证接纳屏障不结算活动 Run，不访问真实资源。"""
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from easel.control.store import Store

with tempfile.TemporaryDirectory(prefix="easel-r1-barrier-review-") as temporary:
    with Store.open(Path(temporary) / "control.sqlite3") as store:
        actor = store.create_user("temporary-review")
        conversation = store.create_conversation(actor, "临时测试")
        run = store.reserve_run(actor, conversation.id, "request-1", "digest-1", 1)
        store.mark_running(actor, run.id)
        store.set_admission_barrier("update-abcdef123456", "desktop-update", 1000)
        status = store.drain_status(1001)
        print(json.dumps({"activeRuns": status["activeRuns"],
                          "settled": status["settled"],
                          "barrier_present": store.admission_barrier() is not None}))
print("TEMP_CLEANUP=ok; LIVE_RESOURCES_TOUCHED=false")
